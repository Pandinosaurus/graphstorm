"""RGNN for node tasks
"""
import os
import time
import torch as th

import torch.nn.functional as F
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel
import numpy as np
import dgl
import abc
import psutil

from .rgnn import GSgnnBase
from .extract_node_embeddings import prepare_batch_input
from .utils import save_embeddings as save_node_embeddings


class GSgnnNodeModel(GSgnnBase):
    """ RGNN model for node tasks

    Parameters
    ----------
    g: DGLGraph
        The graph used in training and testing
    config: GSConfig
        The graphstorm GNN configuration
    task_tracker: GSTaskTrackerAbc
        Task tracker used to log task progress
    train_task: bool
        Whether it is a training task
    """
    def __init__(self, g, config, task_tracker=None, train_task=True):
        super(GSgnnNodeModel, self).__init__(g, config, task_tracker, train_task)
        self.predict_ntype = config.predict_ntype
        self.save_predict_path = config.save_predict_path
        self.alpha_l2norm = config.alpha_l2norm

    def inference(self, target_nidx):
        '''This performs inference on the target nodes.

        Parameters
        ----------
        tartet_ndix : tensor
            The node IDs of the predict node type where we perform prediction.

        Returns
        -------
        tensor
            The prediction results.
        '''
        g = self._g
        device = 'cuda:%d' % self.dev_id
        outputs = self.compute_embeddings(g, device, {self.predict_ntype: target_nidx})
        outputs = outputs[self.predict_ntype]

        return self.predict(self.decoder(outputs[0:len(outputs)]))

    def fit(self, loader, train_data):
        g = self._g
        device = 'cuda:%d' % self.dev_id
        gnn_encoder = self.gnn_encoder
        decoder = self.decoder
        embed_layer = self.embed_layer
        combine_optimizer = self.combine_optimizer

        # training loop
        print("start training...")
        dur = []
        total_steps = 0
        num_input_nodes = 0
        gnn_forward_time = 0
        back_time = 0
        early_stop = False # used when early stop is True
        for epoch in range(self.n_epochs):
            if gnn_encoder is not None:
                gnn_encoder.train()
            if embed_layer is not None:
                embed_layer.train()
            t0 = time.time()

            for i, (input_nodes, seeds, blocks) in enumerate(loader):
                total_steps += 1

                # in the case of a graph with a single node type the returned seeds will not be
                # a dictionary but a tensor of integers this is a possible bug in the DGL code.
                # Otherwise we will select the seeds that correspond to the category node type
                if type(seeds) is dict:
                    seeds = seeds[self.predict_ntype]     # we only predict the nodes with type "category"
                if type(input_nodes) is not dict:
                    input_nodes = {self.predict_ntype: input_nodes}
                for _, nodes in input_nodes.items():
                    num_input_nodes += nodes.shape[0]
                batch_tic = time.time()

                gnn_embs, gnn_forward_time = \
                    self.encoder_forward(blocks, input_nodes, gnn_forward_time, epoch)

                emb = gnn_embs[self.predict_ntype]
                logits = decoder(emb)

                lbl = train_data.labels[seeds].to(device)

                # add regularization loss to all parameters to avoid the unused parameter errors
                pred_loss = self.loss_func(logits, lbl)

                reg_loss = th.tensor(0.).to(device)
                # L2 regularization of dense parameters
                for d_para in self.get_dense_params():
                    reg_loss += d_para.square().sum()

                # weighted addition to the total loss
                total_loss = pred_loss + self.alpha_l2norm * reg_loss

                t3 = time.time()
                gnn_loss = pred_loss.item()
                combine_optimizer.zero_grad()
                total_loss.backward()
                combine_optimizer.step()
                back_time += (time.time() - t3)

                train_score = self.evaluator.compute_score(self.predict(logits), lbl)

                self.log_metric("Train loss", total_loss.item(), total_steps, report_step=total_steps)
                for metric in  self.evaluator.metric:
                    self.log_metric("Train {}".format(metric), train_score[metric], total_steps, report_step=total_steps)

                if i % 20 == 0 and g.rank() == 0:
                    if self.verbose:
                        self.print_info(epoch, i,  num_input_nodes / 20,
                                        (gnn_forward_time / 20, back_time / 20))
                    print("Part {} | Epoch {:05d} | Batch {:03d} | Total_Train Loss (ALL|GNN): {:.4f}|{:.4f} | Time: {:.4f}".
                            format(g.rank(), epoch, i,  total_loss.item(), gnn_loss, time.time() - batch_tic))
                    for metric in self.evaluator.metric:
                        print("Train {}: {:.4f}".format(metric, train_score[metric]))
                    num_input_nodes = gnn_forward_time = back_time = 0

                val_score = None
                if self.evaluator is not None and \
                    self.evaluator.do_eval(total_steps, epoch_end=False):
                    val_score = self.eval(g.rank(), train_data, total_steps)

                    if self.evaluator.do_early_stop(val_score):
                        early_stop = True

                # Every n iterations, check to save the top k models. If has validation score, will save
                # the best top k. But if no validation, will either save the last k model or all models
                # depends on the setting of top k
                if self.save_model_per_iters > 0 and i % self.save_model_per_iters == 0 and i != 0:
                    self.save_topk_models(epoch, i, g, val_score)

                # early_stop, exit current interation.
                if early_stop is True:
                    break

            # end of an epoch
            th.distributed.barrier()
            epoch_time = time.time() - t0
            if g.rank() == 0:
                print("Epoch {} take {}".format(epoch, epoch_time))
            dur.append(epoch_time)

            val_score = None
            if self.evaluator is not None and self.evaluator.do_eval(total_steps, epoch_end=True):
                val_score = self.eval(g.rank(), train_data, total_steps)
                if self.evaluator.do_early_stop(val_score):
                    early_stop = True

            # After each epoch, check to save the top k models. If has validation score, will save
            # the best top k. But if no validation, will either save the last k model or all models
            # depends on the setting of top k. To show this is after epoch save, set the iteration
            # to be None, so that we can have a determistic model folder name for testing and debug.
            self.save_topk_models(epoch, None, g, val_score)

            # early_stop, exit training
            if early_stop is True:
                break

        if g.rank() == 0:
            if self.verbose:
                if self.evaluator is not None:
                    self.evaluator.print_history()

        print("Peak Mem alloc: {:.4f} MB".format(th.cuda.max_memory_allocated(device) / 1024 /1024))
        if g.rank() == 0:
            output = dict(best_test_score=self.evaluator.best_test_score,
                          best_val_score=self.evaluator.best_val_score,
                          peak_mem_alloc_MB=th.cuda.max_memory_allocated(device) / 1024 / 1024)
            print(output)

            if self.verbose:
                # print top k info only when required because sometime the top k is just the last k
                print(f'Top {len(self.topklist.toplist)} ranked models:')
                print([f'Rank {i+1}: epoch-{epoch}' for i, epoch in enumerate(self.topklist.toplist)])

    def eval(self, rank, train_data, total_steps):
        """ do the model evaluation using validiation and test sets

            Parameters
            ----------
            rank: int
                Distributed rank
            train_data: GSgnnNodeTrainData
                Training data
            total_steps: int
                Total number of iterations.

            Returns
            -------
            float: validation score
        """
        teval = time.time()
        target_nidx = th.cat([train_data.val_idx, train_data.test_idx])
        pred = self.inference(target_nidx)

        val_pred, test_pred = th.split(pred,
                                       [len(train_data.val_idx),
                                       len(train_data.test_idx)])
        val_label = train_data.labels[train_data.val_idx]
        val_label = val_label.to(val_pred.device)
        test_label = train_data.labels[train_data.test_idx]
        test_label = test_label.to(test_pred.device)
        val_score, test_score = self.evaluator.evaluate(
            val_pred, test_pred,
            val_label, test_label,
            total_steps)
        if rank == 0:
            self.log_print_metrics(val_score=val_score,
                                    test_score=test_score,
                                    dur_eval=time.time() - teval,
                                    total_steps=total_steps)
        return val_score

    def infer(self, data):
        g = self.g
        device = 'cuda:%d' % self.dev_id

        print("start inference ...")
        # TODO: Make it more efficient
        # We do not need to compute the embedding of all node types
        outputs = self.compute_embeddings(g, device)
        embeddings = outputs[self.predict_ntype]

        # Save prediction result into disk
        if g.rank() == 0:
            predicts = []
            # TODO(xiangsx): Make it distributed (more efficient)
            # The current implementation is only memory efficient
            for start in range(0, len(embeddings), 10240):
                end = start + 10240 if start + 10240 < len(embeddings) else len(embeddings)
                predict = self.predict(self.decoder(embeddings[start:end]))
                predicts.append(predict)
            predicts = th.cat(predicts, dim=0)
            os.makedirs(self.save_predict_path, exist_ok=True)
            th.save(predicts, os.path.join(self.save_predict_path, "predict.pt"))

        th.distributed.barrier()

        # do evaluation if any
        if self.evaluator is not None and \
            self.evaluator.do_eval(0, epoch_end=True):
            test_start = time.time()
            pred = self.predict(self.decoder(embeddings[data.test_idx]))
            labels = data.labels[data.test_idx]
            pred = pred.to(device)
            labels = labels.to(device)

            val_score, test_score = self.evaluator.evaluate(
                pred, pred,
                labels, labels,
                0)
            if g.rank() == 0:
                self.log_print_metrics(val_score=val_score,
                                        test_score=test_score,
                                        dur_eval=time.time() - test_start,
                                        total_steps=0)

        save_embeds_path = self.save_embeds_path
        if save_embeds_path is not None:
            # User may not want to save the node embedding
            # save node embedding
            save_node_embeddings(save_embeds_path,
                embeddings, g.rank(), th.distributed.get_world_size())
            th.distributed.barrier()

""" Infer wrapper for link predicion
"""
import time
import torch as th

from .graphstorm_infer import GSInfer
from ..model.utils import save_embeddings as save_gsgnn_embeddings
from ..model.utils import save_relation_embeddings
from ..model.edge_decoder import LinkPredictDistMultDecoder
from ..model.gnn import do_full_graph_inference
from ..model.lp_gnn import lp_mini_batch_predict

from ..utils import sys_tracker

class GSgnnLinkPredictionInfer(GSInfer):
    """ Link prediction infer.

    This is a highlevel infer wrapper that can be used directly
    to do link prediction model inference.

    Parameters
    ----------
    model : GSgnnNodeModel
        The GNN model for node prediction.
    rank : int
        The rank.
    """

    # TODO(zhengda) We only support full-graph inference for now.
    def infer(self, data, loader, save_embed_path, edge_mask_for_gnn_embeddings='train_mask'):
        """ Do inference

        The inference can do two things:
        1. (Optional) Evaluate the model performance on a test set if given
        2. Generate node embeddings

        Parameters
        ----------
        data: GSgnnData
            The GraphStorm dataset
        loader : GSgnnLinkPredictionTestDataLoader
            The mini-batch sampler for link prediction task.
        save_embed_path : str
            The path where the GNN embeddings will be saved.
        edge_mask_for_gnn_embeddings : str
            The mask that indicates the edges used for computing GNN embeddings. By default,
            the dataloader uses the edges in the training graphs to compute GNN embeddings to
            avoid information leak for link prediction.
        """
        sys_tracker.check('start inferencing')
        embs = do_full_graph_inference(self._model, data,
                                       edge_mask=edge_mask_for_gnn_embeddings,
                                       task_tracker=self.task_tracker)
        sys_tracker.check('compute embeddings')
        if save_embed_path is not None:
            save_gsgnn_embeddings(save_embed_path, embs, self.rank,
                th.distributed.get_world_size())
        th.distributed.barrier()
        sys_tracker.check('save embeddings')

        if self.evaluator is not None:
            test_start = time.time()
            device = th.device(f"cuda:{self.dev_id}") \
                if self.dev_id >= 0 else th.device("cpu")
            test_scores = lp_mini_batch_predict(self._model, embs, loader, device)
            val_mrr, test_mrr = self.evaluator.evaluate(None, test_scores, 0)
            sys_tracker.check('run evaluation')
            if self.rank == 0:
                self.log_print_metrics(val_score=val_mrr,
                                       test_score=test_mrr,
                                       dur_eval=time.time() - test_start,
                                       total_steps=0)

        th.distributed.barrier()
        # save relation embedding if any
        if self.rank == 0:
            decoder = self._model.decoder
            if isinstance(decoder, LinkPredictDistMultDecoder):
                if save_embed_path is not None:
                    save_relation_embeddings(save_embed_path, decoder)

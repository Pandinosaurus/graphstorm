""" Infer wrapper for node classification and regression
"""
import time
import os
import torch as th

from .graphstorm_infer import GSInfer
from ..model.utils import save_embeddings as save_gsgnn_embeddings
from ..model.gnn import do_full_graph_inference
from ..model.node_gnn import node_mini_batch_gnn_predict
from ..model.node_gnn import node_mini_batch_predict

from ..utils import sys_tracker

class GSgnnNodePredictionInfer(GSInfer):
    """ Node classification/regression infer.

    This is a highlevel infer wrapper that can be used directly
    to do node classification/regression model inference.

    Parameters
    ----------
    model : GSgnnNodeModel
        The GNN model for node prediction.
    rank : int
        The rank.
    """

    def infer(self, loader, save_embeds_path, save_predict_path=None,
              mini_batch_infer=False):
        """ Do inference

        The inference does three things:
        1. (Optional) Evaluate the model performance on a test set if given
        2. Generate node embeddings
        3. Comput inference results for nodes with target node type.

        Parameters
        ----------
        loader : GSNodeDataLoader
            The mini-batch sampler for node prediction task.
        save_embeds_path : str
            The path where the GNN embeddings will be saved.
        save_predict_path : str
            The path where the prediction results will be saved.
        mini_batch_infer : bool
            Whether or not to use mini-batch inference.
        """
        do_eval = self.evaluator is not None
        sys_tracker.check('start inferencing')
        if mini_batch_infer:
            res = node_mini_batch_gnn_predict(self._model, loader, return_label=do_eval)
            pred = res[0]
            embs = res[1]
            label = res[2] if do_eval else None
        else:
            embs = do_full_graph_inference(self._model, loader.data,
                                           task_tracker=self.task_tracker)
            res = node_mini_batch_predict(self._model, embs, loader, return_label=do_eval)
            pred = res[0]
            label = res[1] if do_eval else None
        sys_tracker.check('compute embeddings')

        embeddings = {ntype: embs[ntype] for ntype in loader.data.eval_ntypes}
        if save_embeds_path is not None:
            save_gsgnn_embeddings(save_embeds_path,
                embeddings, self.rank, th.distributed.get_world_size())
            th.distributed.barrier()
        sys_tracker.check('save embeddings')

        if save_predict_path is not None:
            os.makedirs(save_predict_path, exist_ok=True)
            th.save(pred, os.path.join(save_predict_path, "predict-{}.pt".format(self.rank)))
        th.distributed.barrier()
        sys_tracker.check('save predictions')

        # do evaluation if any
        if do_eval:
            test_start = time.time()
            val_score, test_score = self.evaluator.evaluate(pred, pred, label, label, 0)
            sys_tracker.check('run evaluation')
            if self.rank == 0:
                self.log_print_metrics(val_score=val_score,
                                       test_score=test_score,
                                       dur_eval=time.time() - test_start,
                                       total_steps=0)

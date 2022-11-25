"""Node classification based on RGNN
"""
import torch as th
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel

from .rgnn_node_base import GSgnnNodeModel
from .node_decoder import EntityClassifier

class GSgnnNodeClassModel(GSgnnNodeModel):
    """ RGNN node classification model

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
        super(GSgnnNodeClassModel, self).__init__(g, config, task_tracker, train_task)

        # node classification related
        self.multilabel = config.multilabel
        self.multilabel_weights = config.multilabel_weights
        self.imbalance_class_weights = config.imbalance_class_weights
        self.num_classes = config.num_classes

        self.model_conf = {
            'task': 'node_classification',
            'predict_ntype': self.predict_ntype,
            'multilabel': self.multilabel,
            'num_classes': self.num_classes,

            # GNN
            'gnn_model': self.gnn_model_type,
            'num_layers': self.n_layers,
            'hidden_size': self.n_hidden,
            'num_bases': self.n_bases,
            'dropout': self.dropout,
            'use_self_loop': self.use_self_loop,
        }
        # logging all the params of this experiment
        self.log_params(config.__dict__)

    def init_dist_decoder(self, train):
        dev_id = self.dev_id
        decoder = EntityClassifier(self.n_hidden, self.num_classes)
        decoder = decoder.to(dev_id)
        self.decoder = DistributedDataParallel(decoder, device_ids=[dev_id],
                                               output_device=dev_id,
                                               find_unused_parameters=True)

    def init_gsgnn_model(self, train=True):
        ''' Initialize the GNN model.

        Argument
        --------
        train : bool
            Indicate whether the model is initialized for training.
        '''
        super(GSgnnNodeClassModel, self).init_gsgnn_model(train)
        loss_func = nn.BCEWithLogitsLoss(pos_weight=self.multilabel_weights) \
            if self.multilabel else \
            nn.CrossEntropyLoss(weight=self.imbalance_class_weights)
        loss_func = loss_func.to(self.dev_id)

        if self.multilabel:
            # BCEWithLogitsLoss wants labels be th.Float
            def lfunc(logits, lbl):
                return loss_func(logits, lbl.type(th.float32))
            self.loss_func = lfunc
        else:
            self.loss_func = loss_func


    def predict(self, logits):
        '''Make prediction on the input logits.

        Parameters
        ----------
        logits : tensor
            The logits generated by the model.

        Returns
        -------
        tensor
            The predicted results.
        '''
        if self.multilabel:
            # multilabel, do nothing
            return logits
        else:
            return logits.argmax(dim=1)

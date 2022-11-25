"""edge regression based on RGNN
"""

from torch import nn
from torch.nn.parallel import DistributedDataParallel

from .rgnn_edge_base import GSgnnEdgeModel
from .edge_decoder import DenseBiDecoder, MLPEdgeDecoder

class GSgnnEdgeRegressModel(GSgnnEdgeModel):
    """ RGNN edge regression model

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
        super(GSgnnEdgeRegressModel, self).__init__(g, config, task_tracker, train_task)

        # decoder related
        # specify the type of decoder
        self.decoder_type = config.decoder_type
        self.num_decoder_basis = config.num_decoder_basis

        self.model_conf = {
            'task': 'edge_regression',
            'target_etype': self.target_etype,
            # GNN
            'gnn_model': self.gnn_model_type,
            'num_layers': self.n_layers,
            'hidden_size': self.n_hidden,
            'num_bases': self.n_bases,
            'dropout': self.dropout,
            'use_self_loop': self.use_self_loop,
        }
        # logging all the params of this experiment

    def init_gsgnn_model(self, train=True):
        ''' Initialize the GNN model.

        Argument
        --------
        train : bool
            Indicate whether the model is initialized for training.
        '''
        super(GSgnnEdgeModel, self).init_gsgnn_model(train)
        mse_loss_func = nn.MSELoss()
        mse_loss_func = mse_loss_func.to(self.dev_id)
        def loss_func(logits, lbl):
            # Make sure the lable is a float tensor
            lbl = lbl.float()
            return mse_loss_func(logits, lbl)
        self.loss_func = loss_func

    def init_dist_decoder(self, train):
        dev_id = self.dev_id
        if self.decoder_type == "DenseBiDecoder":
            decoder = DenseBiDecoder(self.n_hidden, 1,
                                     num_basis=self.num_decoder_basis,
                                     target_etype=self.target_etype,
                                     dropout_rate=self.dropout,
                                     regression=True)
        elif self.decoder_type == "MLPDecoder":
            decoder = MLPEdgeDecoder(2*self.n_hidden, 1,
                                     target_etype=self.target_etype,
                                     regression=True)
        else:
            assert False, "decoder not supported"

        decoder = decoder.to(dev_id)
        # decoder also need to be distributed
        decoder = DistributedDataParallel(decoder,
            device_ids=[dev_id],
            output_device=dev_id,
            find_unused_parameters=True)

        self.decoder = decoder

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
        return logits

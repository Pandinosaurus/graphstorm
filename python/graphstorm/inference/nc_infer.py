""" Infer wrapper for node classification and regression
"""
from ..model import M5GNNNodeClassModel
from ..model import M5gnnAccEvaluator
from ..model import M5GNNNodeRegressModel
from ..model import M5gnnRegressionEvaluator
from .graphstorm_infer import GSInfer
from ..model.dataloading import M5gnnNodeInferData

def get_model_class(config): # pylint: disable=unused-argument
    """ Get model class
    """
    if config.task_type == "node_regression":
        return M5GNNNodeRegressModel, M5gnnRegressionEvaluator
    elif config.task_type == 'node_classification':
        return M5GNNNodeClassModel, M5gnnAccEvaluator
    else:
        raise AttributeError(config.task_type + ' is not supported.')

class M5gnnNodePredictInfer(GSInfer):
    """ Node classification/regression infer.

    This is a highlevel infer wrapper that can be used directly
    to do node classification/regression model inference.

    The infer can do three things:
    1. (Optional) Evaluate the model performance on a test set if given
    2. Generate node embeddings
    3. Comput inference results for nodes with target node type.

    Usage:
    ```
    from m5gnn.config import M5GNNConfig
    from m5gnn.model import M5BertLoader
    from m5gnn.model import M5gnnNodePredictInfer

    config = M5GNNConfig(args)
    bert_config = config.bert_config
    m5_models = M5BertLoader(bert_config).load()

    infer = M5gnnNodePredictInfer(config, m5_models)
    infer.infer()
    ```

    Parameters
    ----------
    config: M5GNNConfig
        Task configuration
    bert_model: dict
        A dict of BERT models in the format of node-type -> M5 BERT model
    """
    def __init__(self, config, bert_model):
        super(M5gnnNodePredictInfer, self).__init__()
        assert isinstance(bert_model, dict)
        self.bert_model = bert_model
        self.config = config

        self.predict_ntype = config.predict_ntype

        # neighbor sample related
        self.eval_fanout = config.eval_fanout \
            if config.model_encoder_type in ["rgat", "rgcn"] else [0]
        self.n_layers = config.n_layers \
            if config.model_encoder_type in ["rgat", "rgcn"] else 1

        self.eval_batch_size = config.eval_batch_size
        self.device = f'cuda:{int(config.local_rank)}'
        self.init_dist_context(config.ip_config,
                               config.graph_name,
                               config.part_config,
                               config.backend)
        self.evaluator = None

    def infer(self):
        """ Do inference
        """
        g = self._g
        part_book = g.get_partition_book()
        config = self.config

        infer_data = M5gnnNodeInferData(g, part_book, self.predict_ntype, config.label_field)

        model_class, eval_class = get_model_class(config)
        np_model = model_class(g, config, self.bert_model, train_task=False)
        np_model.init_m5gnn_model(train=False)

        # if no evalutor is registered, use the default one.
        if self.evaluator is None:
            self.evaluator = eval_class(g, config, infer_data)

        np_model.register_evaluator(self.evaluator)
        if np_model.tracker is not None:
            self.evaluator.setup_tracker(np_model.tracker)
        np_model.infer(infer_data)

    @property
    def g(self):
        """ Get the graph
        """
        return self._g

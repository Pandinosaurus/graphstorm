""" Evaluator for different tasks.
"""
import abc
from statistics import mean
import torch as th

from ..eval import ClassificationMetrics, RegressionMetrics, LinkPredictionMetrics

from .utils import fullgraph_eval
from .utils import broadcast_data
from ..config.config import EARLY_STOP_AVERAGE_INCREASE_STRATEGY
from ..config.config import EARLY_STOP_CONSECUTIVE_INCREASE_STRATEGY

def early_stop_avg_increase_judge(val_score, val_perf_list, comparator):
    """
    Stop the training early if the val_score `decreases` for the last window steps.

    Note: val_score < Average[val scores in last K steps]

    Parameters
    ----------
    val_score: float
        Target validation score.
    val_loss_list: list
        A list holding the history validation scores.
    comparator: operator op
        Comparator

    Returns
    -------
    early_stop : A boolean indicating the early stop
    """
    avg_score = mean(val_perf_list)
    return comparator(val_score, avg_score)

def early_stop_cons_increase_judge(val_score, val_perf_list, comparator):
    """
    Stop the training early if for the last K consecutive steps the validation
    scores are `decreasing`. See the third approach in the Prechelt, L., 1998.
    Early stopping-but when?.
    In Neural Networks: Tricks of the trade (pp. 55-69). Springer, Berlin, Heidelberg.

    Parameters
    ----------
    val_score: float
        Target validation score.
    val_loss_list: list
        A list holding the history validation scores.
    comparator: operator op
        Comparator.

    Returns
    -------
    early_stop : A boolean indicating the early stop.
    """
    early_stop = True
    for old_val_score in val_perf_list:
        early_stop = early_stop and comparator(val_score, old_val_score)

    return early_stop

# TODO(xiangsx): combine GSgnnInstanceEvaluator and GSgnnLPEvaluator
class GSgnnInstanceEvaluator():
    """ Template class for user defined evaluator.

    Parameters
    ----------
    config: GSConfig
        Configurations. Users can add their own configures in the yaml config file.
    train_data: GSgnnTrainData
        The processed training dataset
    """
    def __init__(self, config, train_data):
        # nodes whose embeddings are used during evaluation
        # if None all nodes are used.
        self._history = []
        self.tracker = None
        self._metric = None
        self._best_val_score = None
        self._best_test_score = None
        self._best_iter = None
        self.metrics_obj = None # Evaluation metrics obj

        self.do_validation = train_data.do_validation and not config.no_validation
        self.evaluation_frequency = config.evaluation_frequency
        self._do_early_stop = config.enable_early_stop
        if self._do_early_stop:
            self._call_to_consider_early_stop = config.call_to_consider_early_stop
            self._num_early_stop_calls = 0
            self._window_for_early_stop = config.window_for_early_stop
            self._early_stop_strategy = config.early_stop_strategy
            self._val_perf_list = []

    def setup_tracker(self, client):
        """ Setup evaluation tracker

            Parameters
            ----------
            client:
                tracker client
        """
        self.tracker = client

    @abc.abstractmethod
    def evaluate(self, val_pred, test_pred, val_labels, test_labels, total_iters):
        """
        GSgnnLinkPredictionModel.fit() will call this function to do user defined evalution.

        Parameters
        ----------
        val_pred : tensor
            The tensor stores the prediction results on the validation nodes.
        test_pred : tensor
            The tensor stores the prediction results on the test nodes.
        val_labels : tensor
            The tensor stores the labels of the validation nodes.
        test_labels : tensor
            The tensor stores the labels of the test nodes.
        total_iters: int
            The current interation number.

        Returns
        -----------
        eval_score: float
            Validation score
        test_score: float
            Test score
        """

    def do_eval(self, total_iters, epoch_end=False):
        """ Decide whether to do the evaluation in current iteration or epoch

            Parameters
            ----------
            epoch: int
                The epoch number
            total_iters: int
                The total number of iterations has been taken.
            epoch_end: bool
                Whether it is the end of an epoch

            Returns
            -------
            Whether do evaluation: bool
        """
        if epoch_end and self.do_validation:
            return True
        elif self.evaluation_frequency != 0 and \
            total_iters % self.evaluation_frequency == 0 and \
            self.do_validation:
            return True
        return False


    @abc.abstractmethod
    def compute_score(self, pred, labels):
        """ Compute evaluation score

            Parameters
            ----------
            pred:
                Rediction result
            labels:
                Label
        """

    def print_history(self):
        """ Print history eval info
        """
        for val_score, test_score in self._history:
            print(f"val {self.metric}: {val_score:.3f}, test {self.metric}: {test_score:.3f}")

    def do_early_stop(self, val_score):
        """ Decide whether to stop the training

            Parameters
            ----------
            val_score: float
                Evaluation score
        """
        if self._do_early_stop is False:
            return False

        assert len(val_score) == 1, \
            f"valudation score should be a signle key value pair but got {val_score}"
        self._num_early_stop_calls += 1
        # Not enough existing validation scores
        if self._num_early_stop_calls <= self._call_to_consider_early_stop:
            return False

        val_score = list(val_score.values())[0]
        # Not enough validation scores to make early stop decision
        if len(self._val_perf_list) < self._window_for_early_stop:
            self._val_perf_list.append(val_score)
            return False

        # early stop criteria: if the average evaluation value
        # does not improve in the last N evaluation iterations
        if self._early_stop_strategy == EARLY_STOP_AVERAGE_INCREASE_STRATEGY:
            early_stop = early_stop_avg_increase_judge(val_score,
                self._val_perf_list, self.get_metric_comparator())
        elif self._early_stop_strategy == EARLY_STOP_CONSECUTIVE_INCREASE_STRATEGY:
            early_stop = early_stop_cons_increase_judge(val_score,
                self._val_perf_list, self.get_metric_comparator())
        else:
            return False

        self._val_perf_list.pop(0)
        self._val_perf_list.append(val_score)

        return early_stop

    def get_metric_comparator(self):
        """ Return the comparator of the major eval metric.

            We treat the first metric in all evaluation metrics as the major metric.
        """
        assert self.metrics_obj is not None, \
            "Evaluation metrics object should not be None"
        metric = self.metric[0]
        return self.metrics_obj.metric_comparator[metric]

    @property
    def metric(self):
        """ evaluation metrics
        """
        return self._metric

    @property
    def best_val_score(self):
        """ Best validation score
        """
        return self._best_val_score

    @property
    def best_test_score(self):
        """ Best test score
        """
        return self._best_test_score

    @property
    def best_iter_num(self):
        """ Best iteration number
        """
        return self._best_iter

class GSgnnRegressionEvaluator(GSgnnInstanceEvaluator):
    """ Template class for user defined evaluator.

        Parameters
        ----------
        g: DGLGraph
            The graph used in training and testing
        config: GSConfig
            Configurations. Users can add their own configures in the yaml config file.
        train_data: GSgnnNodeTrainData
            The processed training dataset
    """
    def __init__(self, g, config, train_data): # pylint: disable=unused-argument
        super(GSgnnRegressionEvaluator, self).__init__(config, train_data)
        self._metric = config.eval_metric
        assert len(self.metric) > 0, "At least one metric must be defined"
        self._best_val_score = {}
        self._best_test_score = {}
        self._best_iter = {}
        self.metrics_obj = RegressionMetrics()

        for metric in self.metric:
            self.metrics_obj.assert_supported_metric(metric=metric)
            self._best_val_score[metric] = self.metrics_obj.init_best_metric(metric=metric)
            self._best_test_score[metric] = self.metrics_obj.init_best_metric(metric=metric)
            self._best_iter[metric] = 0

    def evaluate(self, val_pred, test_pred,
        val_labels, test_labels, total_iters):
        """ Compute scores on validation and test predictions.

            Parameters
            ----------
            val_pred : dict of tensors
                The tensor stores the prediction results on the validation nodes.
            test_pred : dict of tensors
                The tensor stores the prediction results on the test nodes.
            val_labels : tensor
                The tensor stores the labels of the validation nodes.
            test_labels : tensor
                The tensor stores the labels of the test nodes.
            total_iters: int
                The current interation number.
            Returns
            -----------
            float
                Validation MSE
            float
                Test MSE
        """
        # exchange preds and labels between runners
        local_rank = th.distributed.get_rank()
        world_size = th.distributed.get_world_size()
        val_pred = broadcast_data(local_rank, world_size, val_pred)
        val_labels = broadcast_data(local_rank, world_size, val_labels)
        test_pred = broadcast_data(local_rank, world_size, test_pred)
        test_labels = broadcast_data(local_rank, world_size, test_labels)

        with th.no_grad():
            val_score = self.compute_score(val_pred, val_labels)
            test_score = self.compute_score(test_pred, test_labels)

        for metric in self.metric:
            # be careful whether > or < it might change per metric.
            if self.metrics_obj.metric_comparator[metric](
                self._best_val_score[metric],val_score[metric]):
                self._best_val_score[metric] = val_score[metric]
                self._best_test_score[metric] = test_score[metric]
                self._best_iter[metric] = total_iters
        self._history.append((val_score, test_score))

        return val_score, test_score

    def compute_score(self, pred, labels):
        """ Compute evaluation score

            Parameters
            ----------
            pred:
                Rediction result
            labels:
                Label

            Returns
            -------
            Evaluation metric values: dict
        """
        scores = {}
        for metric in self.metric:
            scores[metric] = self.metrics_obj.metric_function[metric](pred, labels) \
                    if pred is not None and labels is not None else -1
        return scores

class GSgnnAccEvaluator(GSgnnInstanceEvaluator):
    """ Template class for user defined evaluator.

        Parameters
        ----------
        g: DGLGraph
            The graph used in training and testing
        config: GSConfig
            Configurations. Users can add their own configures in the yaml config file.
        train_data: GSgnnTrainData
            The processed training dataset
    """
    def __init__(self, g, config, train_data): # pylint: disable=unused-argument
        super(GSgnnAccEvaluator, self).__init__(config, train_data)
        self.multilabel = config.multilabel
        self._metric = config.eval_metric
        assert len(self.metric) > 0, \
            "At least one metric must be defined"
        self._best_val_score = {}
        self._best_test_score = {}
        self._best_iter = {}
        self.metrics_obj = ClassificationMetrics(multilabel=self.multilabel)

        for metric in self.metric:
            self.metrics_obj.assert_supported_metric(metric=metric)
            self._best_val_score[metric] = self.metrics_obj.init_best_metric(metric=metric)
            self._best_test_score[metric] = self.metrics_obj.init_best_metric(metric=metric)
            self._best_iter[metric] = 0

    def evaluate(self, val_pred, test_pred, val_labels, test_labels, total_iters):
        """ Compute scores on validation and test predictions.

            Parameters
            ----------
            val_pred : tensor
                The tensor stores the prediction results on the validation nodes.
            test_pred : tensor
                The tensor stores the prediction results on the test nodes.
            val_labels : tensor
                The tensor stores the labels of the validation nodes.
            test_labels : tensor
                The tensor stores the labels of the test nodes.
            total_iters: int
                The current interation number.
            Returns
            -----------
            float
                Validation Score
            float
                Test Score
        """
        # exchange preds and labels between runners
        local_rank = th.distributed.get_rank()
        world_size = th.distributed.get_world_size()
        val_pred = broadcast_data(local_rank, world_size, val_pred)
        val_labels = broadcast_data(local_rank, world_size, val_labels)
        test_pred = broadcast_data(local_rank, world_size, test_pred)
        test_labels = broadcast_data(local_rank, world_size, test_labels)

        with th.no_grad():
            val_score = self.compute_score(val_pred, val_labels, train=False)
            test_score = self.compute_score(test_pred, test_labels, train=False)

        for metric in self.metric:
            # be careful whether > or < it might change per metric.
            if self.metrics_obj.metric_comparator[metric](
                self._best_val_score[metric],val_score[metric]):
                self._best_val_score[metric] = val_score[metric]
                self._best_test_score[metric] = test_score[metric]
                self._best_iter[metric] = total_iters
        self._history.append((val_score, test_score))

        return val_score, test_score

    def compute_score(self, pred, labels, train=True):
        """ Compute evaluation score

            Parameters
            ----------
            pred:
                Rediction result
            labels:
                Label

            Returns
            -------
            Evaluation metric values: dict
        """
        results = {}
        for metric in self.metric:
            if pred is not None and labels is not None:
                if train:
                    # training expects always a single number to be
                    # returned and has a different (potentially) function
                    results[metric] = self.metrics_obj.metric_function[metric](pred, labels)
                else:
                    # validation or testing may have a different
                    # evaluation function, in our case the evaluation code
                    # may return a dictionary with the metric values for each label
                    results[metric] = self.metrics_obj.metric_eval_function[metric](pred, labels)
            else:
                # if the pred is None or the labels is None the metric can not me computed
                results[metric] = -1
        return results

class GSgnnLPEvaluator():
    """ Template class for user defined evaluator.

        Parameters
        ----------
        config: GSConfig
            Configurations. Users can add their own configures in the yaml config file.
        dataset: GSgnnLinkPredictionTrainData
            The processed training dataset
    """
    def __init__(self, config, dataset=None): # pylint: disable=unused-argument
        # nodes whose embeddings are used during evaluation
        # if None all nodes are used.
        self._target_nidx = None
        self.tracker = None
        self._metric = None
        self._best_val_score = None
        self._best_test_score = None
        self._best_iter = None
        self.metrics_obj = None # Evaluation metrics obj

        self.do_validation = dataset.do_validation and not config.no_validation
        self.evaluation_frequency = config.evaluation_frequency
        self._do_early_stop = config.enable_early_stop
        if self._do_early_stop:
            self._call_to_consider_early_stop = config.call_to_consider_early_stop
            self._num_early_stop_calls = 0
            self._window_for_early_stop = config.window_for_early_stop
            self._early_stop_strategy = config.early_stop_strategy
            self._val_perf_list = []

    def setup_tracker(self, client):
        """ Setup evaluation tracker

            Parameters
            ----------
            client:
                tracker client
        """
        self.tracker = client

    @abc.abstractmethod
    def evaluate(self, embeddings, decoder, total_iters, device):
        """
        GSgnnLinkPredictionModel.fit() will call this function to do user defined evalution.

        Parameters
        ----------
        embeddings: dict of tensors
            The node embeddings.
        decoder: Decoder
            Link prediction decoder.
        total_iters: int
            The current interation number.
        device: th.device
            Device to run the evaluation.

        Returns
        -----------
        eval_score: float
            Validation score
        test_score: float
            Test score
        """

    def do_eval(self, total_iters, epoch_end=False):
        """ Decide whether to do the evaluation in current iteration or epoch

            Parameters
            ----------
            epoch: int
                The epoch number
            total_iters: int
                The total number of iterations has been taken.
            epoch_end: bool
                Whether it is the end of an epoch

            Returns
            -------
            Whether do evaluation: bool
        """
        if epoch_end and self.do_validation:
            return True
        elif self.evaluation_frequency != 0 and \
            total_iters % self.evaluation_frequency == 0 and \
            self.do_validation:
            return True
        return False

    def do_early_stop(self, val_score):
        """ Decide whether to stop the training

            Parameters
            ----------
            val_score: float
                Evaluation score
        """
        if self._do_early_stop is False:
            return False

        assert len(val_score) == 1, \
            f"valudation score should be a signle key value pair but got {val_score}"
        self._num_early_stop_calls += 1
        # Not enough existing validation scores
        if self._num_early_stop_calls <= self._call_to_consider_early_stop:
            return False

        val_score = list(val_score.values())[0]
        # Not enough validation scores to make early stop decision
        if len(self._val_perf_list) < self._window_for_early_stop:
            self._val_perf_list.append(val_score)
            return False

        # early stop criteria: if the average evaluation value
        # does not improve in the last N evaluation iterations
        if self._early_stop_strategy == EARLY_STOP_AVERAGE_INCREASE_STRATEGY:
            early_stop = early_stop_avg_increase_judge(val_score,
                self._val_perf_list, self.get_metric_comparator())
        elif self._early_stop_strategy == EARLY_STOP_CONSECUTIVE_INCREASE_STRATEGY:
            early_stop = early_stop_cons_increase_judge(val_score,
                self._val_perf_list, self.get_metric_comparator())

        self._val_perf_list.pop(0)
        self._val_perf_list.append(val_score)

        return early_stop

    def get_metric_comparator(self):
        """ Return the comparator of the major eval metric.

            We treat the first metric in all evaluation metrics as the major metric.
        """

        assert self.metrics_obj is not None, \
            "Evaluation metrics object should not be None"
        metric = self.metric[0]
        return self.metrics_obj.metric_comparator[metric]

    @property
    def target_nidx(self):
        """ target_nidx
        """
        return self._target_nidx

    @property
    def metric(self):
        """ evaluation metrics
        """
        return self._metric

    @property
    def best_val_score(self):
        """ Best validation score
        """
        return self._best_val_score

    @property
    def best_test_score(self):
        """ Best test score
        """
        return self._best_test_score

    @property
    def best_iter_num(self):
        """ Best iteration number
        """
        return self._best_iter

class GSgnnMrrLPEvaluator(GSgnnLPEvaluator):
    """ Template class for user defined evaluator.

    Parameters
    ----------
    g: DGLGraph
        The graph used in training and testing
    config: GSConfig
        Configurations. Users can add their own configures in the yaml config file.
    data: GSgnnLinkPredictionTrainData or GSgnnLinkPredictionInferData
        The processed dataset
    """
    def __init__(self, g, config, data):
        super(GSgnnMrrLPEvaluator, self).__init__(config, data)
        self.g = g
        self.train_idxs = data.train_idxs
        self.val_idxs = data.val_idxs
        self.test_idxs = data.test_idxs
        self.num_negative_edges_eval = config.num_negative_edges_eval
        self.use_dot_product = config.use_dot_product
        # set mlflow_report_frequency only when mlflow_tracker is True
        self.mlflow_report_frequency = \
            config.mlflow_report_frequency if config.mlflow_tracker else 0
        self._metric = ["mrr"]
        assert len(self.metric) > 0, "At least one metric must be defined"

        self.metrics_obj = LinkPredictionMetrics()

        self._best_val_score = {}
        self._best_test_score = {}
        self._best_iter = {}
        for metric in self.metric:
            self._best_val_score[metric] = self.metrics_obj.init_best_metric(metric=metric)
            self._best_test_score[metric] = self.metrics_obj.init_best_metric(metric=metric)
            self._best_iter[metric] = 0

    def evaluate_on_train_set(self, embeddings, decoder, device):
        """ Compute mrr score on training set

            Parameters
            ----------
            embeddings: dict of tensors
                The node embeddings.
            decoder: Decoder
                Link prediction decoder.
            device: th.device
                Device

            Returns
            -------
            train_mrr: float
        """
        assert self.train_idxs is not None, \
            "Must have train_idxs but get None. " \
            "Please check whether the input data is TrainData"
        train_mrr = self.evaluate_on_idx(embeddings, decoder, device,
                                         self.train_idxs,
                                         eval_type="Training")
        return train_mrr

    def _fullgraph_eval(self, g, embeddings, relation_embs, device,
        target_etype, val_idx):
        """ Wraper for fullgraph_eval

            Note: we wrap it so we can do mock test
        """
        return fullgraph_eval(g,
                            embeddings,
                            relation_embs,
                            device,
                            target_etype, val_idx,
                            num_negative_edges_eval=self.num_negative_edges_eval,
                            client=self.tracker,
                            mlflow_report_frequency=self.mlflow_report_frequency)

    def evaluate_on_idx(self, embeddings, decoder, device, val_idxs, eval_type=""):
        """ Compute mrr score on eval or test set

            Parameters
            ----------
            embeddings: dict of tensors
                The node embeddings.
            decoder: Decoder
                Link prediction decoder.
            device: th.device
                Device
            val_idxs: dict of th.Tensor
                Evaluation edge idxs
            eval_type: str
                Used in print: Validation or Testing.

            Returns
            -------
            Mrr score
        """
        g = self.g
        # evaluation idxs is empty.
        # Nothing to do.
        if val_idxs is None or len(val_idxs) == 0:
            return {"mrr": -1}

        val_mrrs = {}
        for target_etype, val_idx in val_idxs.items():
            relation_embs = None if self.use_dot_product \
                else decoder.module.get_relemb(target_etype)
            val_metrics = self._fullgraph_eval(g,
                                                embeddings,
                                                relation_embs,
                                                device,
                                                target_etype,
                                                val_idx)
            # TODO(xiangsx): change evaluation metric names into lower case
            val_mrr = val_metrics['MRR'] # fullgraph_eval use 'MRR' as keyword
            val_mrrs[target_etype] = val_mrr
        th.distributed.barrier()
        if g.rank() == 0:
            print(f"{eval_type} metrics: {val_metrics}")
            print(f"{eval_type} mrr: {val_mrrs}")
        val_mrrs_all = []
        # Average mrr across edges under different target etypes
        for _, mrr_val in val_mrrs.items():
            val_mrrs_all.append(mrr_val)
        val_mrr = sum(val_mrrs_all) / len(val_mrrs_all)

        # TODO add more metrics here
        return {"mrr": val_mrr}

    def evaluate(self, embeddings, decoder, total_iters, device):
        """ GSgnnLinkPredictionModel.fit() will call this function to do user defined evalution.

            Parameters
            ----------
            embeddings: dict of tensors
                The node embeddings.
            decoder: Decoder
                Link prediction decoder.
            total_iters: int
                The current interation number.
            device: th.device
                Device to run the evaluation.

            Returns
            -----------
            val_mrr: float
                Validation mrr
            test_mrr: float
                Test mrr
        """
        g = self.g
        test_score = self.evaluate_on_idx(
            embeddings, decoder, device, self.test_idxs,  eval_type="Testing")

        # If val_idxs is None, (It is inference only task)
        # we do not need to calculate validation score.
        # Furthermore, we do not need to record the best val_score and iter.
        if self.val_idxs is not None:
            val_score = self.evaluate_on_idx(
                embeddings, decoder, device, self.val_idxs, eval_type="Validation")
            # Wait for all trainers to finish their work.

            if g.rank() == 0:
                for metric in self.metric:
                    # be careful whether > or < it might change per metric.
                    if self.metrics_obj.metric_comparator[metric](
                        self._best_val_score[metric], val_score[metric]):
                        self._best_val_score[metric] = val_score[metric]
                        self._best_test_score[metric] = test_score[metric]
                        self._best_iter[metric] = total_iters
        else:
            val_score = {"mrr": -1} # Dummy

        return val_score, test_score

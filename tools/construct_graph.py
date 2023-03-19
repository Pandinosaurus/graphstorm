""" Preprocess the input data """
from multiprocessing import Process
import multiprocessing
import glob
import os
import dgl
import json
import pyarrow.parquet as pq
import pyarrow as pa
import numpy as np
from functools import partial
import argparse

from transformers import BertTokenizer
import torch as th

##################### The I/O functions ####################

def read_data_parquet(data_file):
    """ Read data from the parquet file.

    Parameters
    ----------
    data_file : str
        The parquet file that contains the data

    Returns
    -------
    dict : map from data name to data.
    """
    table = pq.read_table(data_file)
    pd = table.to_pandas()
    data = {}
    for key in pd:
        d = np.array(pd[key])
        # For multi-dimension arrays, we split them by rows and
        # save them as objects in parquet. We need to merge them
        # together and store them in a tensor.
        if d.dtype.hasobject:
            d = [d[i] for i in range(len(d))]
            d = np.stack(d)
        data[key] = d
    return data

def write_data_parquet(data, data_file):
    df = {}
    for key in data:
        arr = data[key]
        assert len(arr.shape) == 1 or len(arr.shape) == 2
        if len(arr.shape) == 1:
            df[key] = arr
        else:
            df[key] = [arr[i] for i in range(len(arr))]
    table = pa.Table.from_arrays(list(df.values()), names=list(df.keys()))
    pq.write_table(table, data_file)

def parse_file_format(fmt):
    """ Parse the file format blob

    Parameters
    ----------
    fmt : dict
        Describe the file format.
    """
    if fmt["name"] == "parquet":
        return read_data_parquet
    else:
        raise ValueError('Unknown file format: {}'.format(fmt['name']))

############## The functions for parsing configurations #############

def parse_tokenize(op):
    """ Parse the tokenization configuration

    The parser returns a function that tokenizes text with HuggingFace tokenizer.
    The tokenization function returns a dict of three Pytorch tensors.

    Parameters
    ----------
    op : dict
        The configuration for the operation.

    Returns
    -------
    callable : a function to process the data.
    """
    tokenizer = BertTokenizer.from_pretrained(op['bert_model'])
    max_seq_length = int(op['max_seq_length'])
    def tokenize(file_idx, strs):
        tokens = []
        att_masks = []
        type_ids = []
        for s in strs:
            t = tokenizer(s, max_length=max_seq_length,
                          truncation=True, padding='max_length', return_tensors='pt')
            tokens.append(t['input_ids'])
            att_masks.append(t['attention_mask'])
            type_ids.append(t['token_type_ids'])
        return {'token_ids': th.cat(tokens, dim=0),
                'attention_mask': th.cat(att_masks, dim=0),
                'token_type_ids': th.cat(type_ids, dim=0)}
    return tokenize

def parse_feat_ops(confs):
    """ Parse the configurations for processing the features

    The feature transformation:
    {
        "feature_col":  ["<column name>", ...],
        "feature_name": "<feature name>",
        "data_type":    "<feature data type>",
        "transform":    {"name": "<operator name>", ...}
    }

    Parameters
    ----------
    confs : list
        A list of feature transformations.

    Returns
    -------
    list of tuple : The operations
    """
    ops = []
    for feat in confs:
        dtype = None
        if 'transform' not in feat:
            transform = None
        elif transform['name'] == 'tokenize_hf':
            trasnform = parse_tokenize(transform)
        else:
            raise ValueError('Unknown operation: {}'.format(transform['name']))
        ops.append((feat['feature_col'], feat['feature_name'], dtype, transform))
    return ops

#################### The main function for processing #################

def process_features(data, ops):
    """ Process the data with the specified operations.

    This function runs the input operations on the corresponding data
    and returns the processed results.

    Parameters
    ----------
    data : dict
        The data stored as a dict.
    ops : list of tuples
        The operations. Each tuple contains two elements. The first element
        is the data name and the second element is a Python function
        to process the data.

    Returns
    -------
    dict : the key is the data name, the value is the processed data.
    """
    new_data = {}
    for feat_col, feat_name, dtype, op in ops:
        # If the transformation is defined on the feature.
        if op is not None:
            res = op(data[feat_col])
        # If the required data type is defined on the feature.
        elif dtype is not None:
            res = data[feat_col].astype(dtype)
        # If no transformation is defined for the feature.
        else:
            res = data[feat_col]
        new_data[feat_name] = res
    return new_data

def process_labels(data, label_confs):
    """ Process labels

    Parameters
    ----------
    data : dict
        The data stored as a dict.
    label_conf : list of dict
        The list of configs to construct labels.
    """
    assert len(label_confs) == 1
    label_conf = label_confs[0]
    label_col = label_conf['label_col']
    label = data[label_conf['label_col']]
    if label_conf['task_type'] == 'classification':
        label = np.int32(label)
    if 'split_type' in label_conf:
        train_split, val_split, test_split = label_conf['split_type']
        rand_idx = np.random.permutation(len(label))
        train_idx = rand_idx[0:int(len(label) * train_split)]
        val_idx = rand_idx[int(len(label) * train_split):int(len(label) * (train_split + val_split))]
        test_idx = rand_idx[int(len(label) * (train_split + val_split)):]
        train_mask = np.zeros((len(label),), dtype=np.int8)
        val_mask = np.zeros((len(label),), dtype=np.int8)
        test_mask = np.zeros((len(label),), dtype=np.int8)
        train_mask[train_idx] = 1
        val_mask[val_idx] = 1
        test_mask[test_idx] = 1
    return {label_col: label,
            'train_mask': train_mask,
            'val_mask': val_mask,
            'test_mask': test_mask}

################### The functions for multiprocessing ###############

def wait_process(q, max_proc):
    """ Wait for a process

    Parameters
    ----------
    q : list of process
        The list of processes
    max_proc : int
        The maximal number of processes to process the data together.
    """
    if len(q) < max_proc:
        return
    q[0].join()
    q.pop(0)
    
def wait_all(q):
    """ Wait for all processes

    Parameters
    ----------
    q : list of processes
        The list of processes
    """
    for p in q:
        p.join()
        
def get_in_files(in_files):
    """ Get the input files.

    The input file string may contains a wildcard. This function
    gets all files that meet the requirement.

    Parameters
    ----------
    in_files : a str or a list of str
        The input files.

    Returns
    -------
    a list of str : the full name of input files.
    """
    if '*' in in_files:
        in_files = glob.glob(in_files)
    elif not isinstance(in_files, list):
        in_files = [in_files]
    in_files.sort()
    return in_files

def parse_node_data(i, in_file, feat_ops, node_id_col, label_conf,
                    read_file, return_dict):
    data = read_file(in_file)
    feat_data = process_features(data, feat_ops) if feat_ops is not None else {}
    if label_conf is not None:
        label_data = process_labels(data, label_conf)
        for key, val in label_data.items():
            feat_data[key] = val
    return_dict[i] = (data[node_id_col], feat_data)

def parse_edge_data(i, in_file, feat_ops, src_id_col, dst_id_col, edge_type,
                    node_id_map, label_conf, read_file, return_dict):
    data = read_file(in_file)
    feat_data = process_features(data, feat_ops) if feat_ops is not None else {}
    if label_conf is not None:
        label_data = process_labels(data, label_conf)
        for key, val in label_data.items():
            feat_data[key] = val
    src_ids = data[src_id_col]
    dst_ids = data[dst_id_col]
    assert node_id_map is not None
    src_type, _, dst_type = edge_type
    if src_type in node_id_map:
        src_ids = np.array([node_id_map[src_type][sid] for sid in src_ids])
    else:
        assert np.issubdtype(src_ids.dtype, np.integer), \
                "The source node Ids have to be integer."
    if dst_type in node_id_map:
        dst_ids = np.array([node_id_map[dst_type][did] for did in dst_ids])
    else:
        assert np.issubdtype(dst_ids.dtype, np.integer), \
                "The destination node Ids have to be integer."
    return_dict[i] = (src_ids, dst_ids, feat_data)

def create_id_map(ids):
    return {id1: i for i, id1 in enumerate(ids)}

def process_node_data(process_confs, remap_id):
    """ Process node data

    We need to process all node data before we can process edge data.
    Processing node data will generate the ID mapping.

    The node data of a node type is defined as follows:
    {
        "node_id_col":  "<column name>",
        "node_type":    "<node type>",
        "format":       {"name": "csv", "separator": ","},
        "files":        ["<paths to files>", ...],
        "features":     [
            {
                "feature_col":  ["<column name>", ...],
                "feature_name": "<feature name>",
                "data_type":    "<feature data type>",
                "transform":    {"name": "<operator name>", ...}
            },
        ],
        "labels":       [
            {
                "label_col":    "<column name>",
                "task_type":    "<task type: e.g., classification>",
                "split_type":   [0.8, 0.2, 0.0],
                "custom_train": "<the file with node IDs in the train set>",
                "custom_valid": "<the file with node IDs in the validation set>",
                "custom_test":  "<the file with node IDs in the test set>",
            },
        ],
    }

    Parameters
    ----------
    process_confs: list of dicts
        The configurations to process node data.
    remap_id: bool
        Whether or not to remap node IDs

    Returns
    -------
    dict: node ID map
    dict: node features.
    """
    node_data = {}
    node_id_map = {}
    for process_conf in process_confs:
        node_id_col = process_conf['node_id_col']
        node_type = process_conf['node_type']
        feat_ops = parse_feat_ops(process_conf['features'])
        feat_names = [feat_op['feature_name'] for feat_op in process_conf['features']]
        q = []
        manager = multiprocessing.Manager()
        return_dict = manager.dict()
        read_file = parse_file_format(process_conf['format'])
        in_files = get_in_files(process_conf['files'])
        label_conf = process_conf['labels'] if 'labels' in process_conf else None
        for i, in_file in enumerate(in_files):
            p = Process(target=parse_node_data, args=(i, in_file, feat_ops, node_id_col,
                                                      label_conf, read_file, return_dict))
            p.start()
            q.append(p)
            wait_process(q, num_processes)
        wait_all(q)

        type_node_id_map = [None] * len(return_dict)
        type_node_data = {}
        for i, (node_ids, data) in return_dict.items():
            for feat_name in data:
                if feat_name not in type_node_data:
                    type_node_data[feat_name] = [None] * len(return_dict)
                type_node_data[feat_name][i] = data[feat_name]
            type_node_id_map[i] = node_ids

        assert type_node_id_map[0] is not None
        type_node_id_map = np.concatenate(type_node_id_map)
        # We don't need to create ID map if the node IDs are integers,
        # all node Ids are in sequence start from 0 and
        # the user doesn't force to remap node IDs.
        if np.issubdtype(type_node_id_map.dtype, np.integer) \
                and np.all(type_node_id_map == np.arange(len(type_node_id_map))) \
                and not remap_id:
            num_nodes = len(type_node_id_map)
            type_node_id_map = None
        else:
            num_nodes = len(type_node_id_map)
            type_node_id_map = create_id_map(type_node_id_map)

        for feat_name in type_node_data:
            type_node_data[feat_name] = np.concatenate(type_node_data[feat_name])
            assert len(type_node_data[feat_name]) == num_nodes

        node_data[node_type] = type_node_data
        if type_node_id_map is not None:
            node_id_map[node_type] = type_node_id_map

    return (node_id_map, node_data)

def process_edge_data(process_confs, node_id_map):
    """ Process edge data

    The edge data of an edge type is defined as follows:
    {
        "source_id_col":    "<column name>",
        "dest_id_col":      "<column name>",
        "relation":         "<src type, relation type, dest type>",
        "format":           {"name": "csv", "separator": ","},
        "files":            ["<paths to files>", ...],
        "features":         [
            {
                "feature_col":  ["<column name>", ...],
                "feature_name": "<feature name>",
                "data_type":    "<feature data type>",
                "transform":    {"name": "<operator name>", ...}
            },
        ],
        "labels":           [
            {
                "label_col":    "<column name>",
                "task_type":    "<task type: e.g., classification>",
                "split_type":   [0.8, 0.2, 0.0],
                "custom_train": "<the file with node IDs in the train set>",
                "custom_valid": "<the file with node IDs in the validation set>",
                "custom_test":  "<the file with node IDs in the test set>",
            },
        ],
    }

    Parameters
    ----------
    process_confs: list of dicts
        The configurations to process edge data.
    node_id_map: dict
        The node ID map.

    Returns
    -------
    dict: edge features.
    """
    edges = {}
    edge_data = {}

    for process_conf in process_confs:
        src_id_col = process_conf['source_id_col']
        dst_id_col = process_conf['dest_id_col']
        edge_type = process_conf['relation']
        feat_ops = parse_feat_ops(process_conf['features']) \
                if 'features' in process_conf else None
        feat_names = [feat_op['feature_name'] for feat_op in process_conf['features']] \
                if feat_ops is not None else None
        q = []
        manager = multiprocessing.Manager()
        return_dict = manager.dict()
        read_file = parse_file_format(process_conf['format'])
        in_files = get_in_files(process_conf['files'])
        label_conf = process_conf['labels'] if 'labels' in process_conf else None
        for i, in_file in enumerate(in_files):
            p = Process(target=parse_edge_data, args=(i, in_file, feat_ops,
                                                      src_id_col, dst_id_col, edge_type,
                                                      node_id_map, label_conf,
                                                      read_file, return_dict))
            p.start()
            q.append(p)
            wait_process(q, num_processes)
        wait_all(q)

        type_src_ids = [None] * len(return_dict)
        type_dst_ids = [None] * len(return_dict)
        type_edge_data = {}
        for i, (src_ids, dst_ids, part_data) in return_dict.items():
            type_src_ids[i] = src_ids
            type_dst_ids[i] = dst_ids
            for feat_name in part_data:
                if feat_name not in type_edge_data:
                    type_edge_data[feat_name] = [None] * len(return_dict)
                type_edge_data[feat_name][i] = part_data[feat_name]

        type_src_ids = np.concatenate(type_src_ids)
        type_dst_ids = np.concatenate(type_dst_ids)
        assert len(type_src_ids) == len(type_dst_ids)

        for feat_name in type_edge_data:
            type_edge_data[feat_name] = np.concatenate(type_edge_data[feat_name])
            assert len(type_edge_data[feat_name]) == len(type_src_ids)

        edge_type = tuple(edge_type)
        edges[edge_type] = (type_src_ids, type_dst_ids)
        edge_data[edge_type] = type_edge_data

    return edges, edge_data

if __name__ == '__main__':
    argparser = argparse.ArgumentParser("Preprocess graphs")
    argparser.add_argument("--conf_file", type=str, required=True,
            help="The configuration file.")
    argparser.add_argument("--num_processes", type=int, default=1,
            help="The number of processes to process the data simulteneously.")
    argparser.add_argument("--output_dir", type=str, required=True,
            help="The path of the output data folder.")
    argparser.add_argument("--graph_name", type=str, required=True,
            help="The graph name")
    argparser.add_argument("--remap_node_id", type=bool, default=False,
            help="Whether or not to remap node IDs.")
    args = argparser.parse_args()
    num_processes = args.num_processes
    process_confs = json.load(open(args.conf_file, 'r'))

    node_id_map, node_data = process_node_data(process_confs['node'], args.remap_node_id)
    edges, edge_data = process_edge_data(process_confs['edge'], node_id_map)
    num_nodes = {}
    for ntype in node_data:
        for feat_name in node_data[ntype]:
            num_nodes[ntype] = len(node_data[ntype][feat_name])
    g = dgl.heterograph(edges, num_nodes_dict=num_nodes)
    for ntype in node_data:
        for name, data in node_data[ntype].items():
            g.nodes[ntype].data[name] = th.tensor(data)
    for etype in edge_data:
        for name, data in edge_data[etype].items():
            g.edges[etype].data[name] = th.tensor(data)

    dgl.save_graphs(os.path.join(args.output_dir, args.graph_name + ".dgl"), [g])
    for ntype in node_id_map:
        map_data = {}
        map_data["orig"] = np.array(list(node_id_map[ntype].keys()))
        map_data["new"] = np.array(list(node_id_map[ntype].values()))
        write_data_parquet(map_data, os.path.join(args.output_dir, ntype + "_id_remap.parquet"))
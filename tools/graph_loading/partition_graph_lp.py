"""
    Copyright 2023 Contributors

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

    Generate example graph data using built-in datasets for link prediction.
"""

import os
import dgl
import torch as th
import argparse
import time

from graphstorm.data import OGBTextFeatDataset
from graphstorm.data import MovieLens100kNCDataset
from graphstorm.data import ConstructedGraphDataset
from graphstorm.data import MAGLSCDataset
from graphstorm.utils import sys_tracker

if __name__ == '__main__':
    argparser = argparse.ArgumentParser("Partition DGL graphs for link prediction "
                                        + "tasks only.")
    # dataset and file arguments
    argparser.add_argument("-d", "--dataset", type=str, required=True,
                           help="dataset to use")
    argparser.add_argument("--input_folder", type=str, default=None)
    # link prediction arguments
    argparser.add_argument('--predict_etypes', type=str, help='The canonical edge types for making'
                           + ' prediction. Multiple edge types can be separated by " ". '
                           + 'For example, "EntA,Links,EntB EntC,Links,EntD"')
    # label split arguments
    argparser.add_argument('--train_pct', type=float, default=0.8,
                           help='The pct of train nodes/edges. Should be > 0 and < 1.')
    argparser.add_argument('--val_pct', type=float, default=0.1,
                           help='The pct of validation nodes/edges. Should be > 0 and < 1.')
    # graph modification arguments
    argparser.add_argument('--undirected', action='store_true',
                           help='turn the graph into an undirected graph.')
    argparser.add_argument('--train_graph_only', action='store_true',
                           help='Only partition the training graph.')
    argparser.add_argument('--retain_original_features',  type=lambda x: (str(x).lower() in ['true', '1']),
                           default=True, help= "whether to use the original features or use the paper title or abstract"
                                                "for the ogbn-arxiv dataset")
    argparser.add_argument('--retain_etypes', nargs='+', type=str, default=[],
        help='The list of canonical etype that will be retained before partitioning the graph. '
              + 'This might be helpfull to remove noise edges in this application. Format example: '
              + '--retain_etypes query,clicks,asin query,adds,asin query,purchases,asin '
              + 'asin,rev-clicks,query asin,rev-adds,query asin,rev-purchases,query')
    # partition arguments
    argparser.add_argument('--num_parts', type=int, default=4,
                           help='number of partitions')
    argparser.add_argument('--part_method', type=str, default='metis',
                           help='the partition method')
    argparser.add_argument('--balance_train', action='store_true',
                           help='balance the training size in each partition.')
    argparser.add_argument('--balance_edges', action='store_true',
                           help='balance the number of edges in each partition.')
    argparser.add_argument('--num_trainers_per_machine', type=int, default=1,
                           help='the number of trainers per machine. The trainer ids are stored\
                                in the node feature \'trainer_id\'')
    # output arguments
    argparser.add_argument('--output', type=str, default='data',
                           help='The output directory to store the partitioned results.')
    argparser.add_argument('--save_mappings', action='store_true',
                           help='Store the mappings for the edges and nodes after partition.')

    args = argparser.parse_args()
    print(args)
    start = time.time()

    constructed_graph = False

    # arugment sanity check
    assert (args.train_pct + args.val_pct) <= 1, \
        "The sum of train and validation percentages should NOT larger than 1."
    edge_pct = args.train_pct + args.val_pct

    # load graph data
    if args.dataset == 'ogbn-arxiv':
        dataset = OGBTextFeatDataset(args.input_folder, args.dataset, edge_pct=edge_pct,
                                     retain_original_features=args.retain_original_features)
    elif args.dataset == 'ogbn-products':
        dataset = OGBTextFeatDataset(args.input_folder, args.dataset, edge_pct=edge_pct,
                                     retain_original_features=args.retain_original_features)
    elif args.dataset == 'movie-lens-100k':
        dataset = MovieLens100kNCDataset(args.input_folder, edge_pct=edge_pct)
    elif args.dataset == 'movie-lens-100k-text':
        dataset = MovieLens100kNCDataset(args.input_folder,
                                         edge_pct=edge_pct, use_text_feat=True)
    elif args.dataset == 'ogbn-papers100M':
        dataset = OGBTextFeatDataset(args.input_folder, dataset=args.dataset, edge_pct=edge_pct,
                                     retain_original_features=args.retain_original_features)
    elif args.dataset == 'mag-lsc':
        dataset = MAGLSCDataset(args.input_folder, edge_pct=edge_pct)
    else:
        constructed_graph = True
        print("Loading user defined dataset " + str(args.dataset))
        dataset = ConstructedGraphDataset(args.dataset, args.input_folder)
        assert args.predict_etypes is not None, "For user defined dataset, you must provide predict_etypes"

    g = dataset[0]

    if constructed_graph:
        if args.undirected:
            print("Creating reverse edges ...")
            edges = {}
            for src_ntype, etype, dst_ntype in g.canonical_etypes:
                src, dst = g.edges(etype=(src_ntype, etype, dst_ntype))
                edges[(src_ntype, etype, dst_ntype)] = (src, dst)
                edges[(dst_ntype, etype + '-rev', src_ntype)] = (dst, src)
            num_nodes_dict = {}
            for ntype in g.ntypes:
                num_nodes_dict[ntype] = g.num_nodes(ntype)
            new_g = dgl.heterograph(edges, num_nodes_dict)
            # Copy the node data and edge data to the new graph. The reverse edges will not have data.
            for ntype in g.ntypes:
                for name in g.nodes[ntype].data:
                    new_g.nodes[ntype].data[name] = g.nodes[ntype].data[name]
            for etype in g.canonical_etypes:
                for name in g.edges[etype].data:
                    new_g.edges[etype].data[name] = g.edges[etype].data[name]
            g = new_g
            new_g = None

    target_etypes = dataset.target_etype if not constructed_graph else \
        [tuple(pred_etype.split(',')) for pred_etype in args.predict_etypes.split(' ')]

    if not isinstance(target_etypes, list):
        target_etypes = [target_etypes]

    if constructed_graph:
        for target_e in target_etypes:
            num_edges = g.num_edges(target_e)
            g.edges[target_e].data['train_mask'] = th.full((num_edges,), False, dtype=th.bool)
            g.edges[target_e].data['val_mask'] = th.full((num_edges,), False, dtype=th.bool)
            g.edges[target_e].data['test_mask'] = th.full((num_edges,), False, dtype=th.bool)
            g.edges[target_e].data['train_mask'][: int(num_edges * args.train_pct)] = True
            g.edges[target_e].data['val_mask'][int(num_edges * args.train_pct): \
                                               int(num_edges * (args.train_pct + args.val_pct))] = True
            g.edges[target_e].data['test_mask'][int(num_edges * (args.train_pct + args.val_pct)): ] = True

    print(f'load {args.dataset} takes {time.time() - start:.3f} seconds')
    print(f'\n|V|={g.number_of_nodes()}, |E|={g.number_of_edges()}\n')
    for target_e in target_etypes:
        train_total = th.sum(g.edges[target_e].data['train_mask']) \
                      if 'train_mask' in g.edges[target_e].data else 0
        val_total = th.sum(g.edges[target_e].data['val_mask']) \
                    if 'val_mask' in g.edges[target_e].data else 0
        test_total = th.sum(g.edges[target_e].data['test_mask']) \
                     if 'test_mask' in g.edges[target_e].data else 0
        print(f'Edge type {target_e} :train: {train_total}, '
              +f'valid: {val_total}, test: {test_total}')

    # Get the train graph.
    if args.train_graph_only:
        sub_edges = {}
        for etype in g.canonical_etypes:
            sub_edges[etype] = g.edges[etype].data['train_mask'].bool() if 'train_mask' in g.edges[etype].data \
                    else th.ones(g.number_of_edges(etype), dtype=th.bool)
        g = dgl.edge_subgraph(g, sub_edges, relabel_nodes=False, store_ids=False)

    retain_etypes = [tuple(retain_etype.split(',')) for retain_etype in args.retain_etypes]

    if len(retain_etypes)>0:
        g = dgl.edge_type_subgraph(g, retain_etypes)
    sys_tracker.check("Finish processing the final graph")
    print(g)
    if args.balance_train and not args.train_graph_only:
        balance_etypes = {target_et: g.edges[target_et].data['train_mask'] for target_et in target_etypes}
    else:
        balance_etypes = None

    new_node_mapping, new_edge_mapping = dgl.distributed.partition_graph(g, args.dataset, args.num_parts, args.output,
                                                                         part_method=args.part_method,
                                                                         balance_edges=args.balance_edges,
                                                                         num_trainers_per_machine=args.num_trainers_per_machine,
                                                                         return_mapping=True)
    sys_tracker.check('partition the graph')
    if args.save_mappings:
        # TODO add something that is more scalable here as a saving method

        # the new_node_mapping contains per entity type on the ith row the original node id for the ith node.
        th.save(new_node_mapping, os.path.join(args.output, "new_node_mapping.pt"))
        # the new_edge_mapping contains per edge type on the ith row the original edge id for the ith edge.
        th.save(new_edge_mapping, os.path.join(args.output, "new_edge_mapping.pt"))

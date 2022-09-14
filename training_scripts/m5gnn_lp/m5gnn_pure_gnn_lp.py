""" M5GNN pure gpu link prediction
"""

import argparse
import dgl

from graphstorm.config import get_argument_parser
from graphstorm.config import M5GNNConfig
from graphstorm.trainer import M5gnnLinkPredictionTrainer

def main(args):
    config = M5GNNConfig(args)
    m5_models = {}

    trainer = M5gnnLinkPredictionTrainer(config, m5_models)
    trainer.fit()

def generate_parser():
    parser = get_argument_parser()
    return parser

if __name__ == '__main__':
    parser=generate_parser()

    args = parser.parse_args()
    print(args)
    main(args)

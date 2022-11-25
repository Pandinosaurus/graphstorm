#!/bin/bash
python3 graphstorm/sagemaker/launch_train.py  --version-tag sagemaker_v3 --training-ecr-repository graphstorm_alpha --account-id ACCOUNT_ID --region us-east-1 --role IAM_ROLE --graph-name ogbn-arxiv --graph-data-s3 S3_PATH_TO_GRAPH_DATA --task-type "link_prediction" --model-artifact-s3 S3_PATH_TO_STORE_SAVED_MODEL --train-yaml-s3 S3_PATH_TO_TRAIN_CONFIG --train-yaml-name arxiv_lp_hf.yaml --n-layers 1 --n-hidden 128 --backend gloo --batch-size 128

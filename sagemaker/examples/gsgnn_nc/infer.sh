#!/bin/sh
python3 graphstorm/sagemaker/launch_infer.py --version-tag sagemaker_v3 --infer-ecr-repository graphstorm_alpha --account-id ACCOUNT_ID --region us-east-1 --role IAM_ROLE --graph-name ogbn-arxiv --graph-data-s3 S3_PATH_TO_GRAPH_DATA --task-type "node_classification" --model-artifact-s3 S3_PATH_TO_MODEL_TO_BE_LOAD --model-sub-path MODEL_CHECKPOINT_NAME --infer-yaml-s3 S3_PATH_TO_INFER_CONFIG --infer-yaml-name arxiv_nc_hf.yaml --emb-s3-path S3_PATH_TO_UPLOAD_EMB --n-layers 1 --n-hidden 128 --backend gloo --batch-size 128

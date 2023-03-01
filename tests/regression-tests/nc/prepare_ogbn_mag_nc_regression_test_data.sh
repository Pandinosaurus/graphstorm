date

GSF_HOME=/graph-storm
mkdir /regression-tests-data
mkdir /regression-tests-data/ogbn-mag-data
REG_DATA_PATH=/regression-tests-data/ogbn-mag-data
export PYTHONPATH=${GSF_HOME}/python/

# Construct the graph with original features
python3 ${GSF_HOME}/tools/gen_mag_dataset.py --savepath ${REG_DATA_PATH}/ogbn-mag/

# Partition the graph
python3 -u $GSF_HOME/tools/partition_graph.py --dataset ogbn-mag \
                                              --filepath ${REG_DATA_PATH}/ogbn-mag/ \
                                              --num_parts 1 \
                                              --num_trainers_per_machine 4 \
                                              --predict_ntype paper \
                                              --nlabel_field paper:labels \
                                              --output ${REG_DATA_PATH}/ogb_mag_nc_train_val_1p_4t

date
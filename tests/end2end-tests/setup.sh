DGL_HOME=/root/dgl

wget https://raw.githubusercontent.com/dmlc/dgl/master/tools/launch.py
mv launch.py $DGL_HOME/tools/launch.py

sh ./tests/end2end-tests/create_data.sh


#!/usr/bin/python3
"""
Prepares all nodes and startup scripts
"""

import shlex
import time
import re
import subprocess
import os
import io
import socket
import json
import requests
import pandas as pd

placeholder = re.compile(r'\<(.+?)\>')

def read_meta_key(key):
    response = requests.get(
        'http://metadata.google.internal/computeMetadata/v1/{key}'.format(key=key),
        headers={
            'Metadata-Flavor': 'Google'
        }
    )
    if response.status_code == 200:
        return response.text
    raise ValueError('({}) : {}'.format(response.status_code, response.text))

def build_node_defs(cluster_name):
    zone = os.path.basename(read_meta_key('instance/zone'))
    n_workers = int(read_meta_key('instance/attributes/canine_conf_n_workers'))
    gpu_type, gpu_count = read_meta_key('instance/attributes/canine_conf_gpus').split(':')
    gpu_count = int(gpu_count)
    with open('/apps/slurm/current/etc/instance_conf.json', 'w') as w:
        json.dump(
            {
                'gpu_type': gpu_type,
                'gpu_count': gpu_count,
                'compute_zone': zone,
                'project': read_meta_key('project/project-id'),
                'sec': read_meta_key('instance/attributes/canine_conf_sec'),
                'cluster': read_meta_key('instance/attributes/canine_conf_cluster_name'),
                'controller': read_meta_key('instance/name'),
                'ip': read_meta_key('instance/attributes/canine_conf_ip') == '+',
                'preemptible': read_meta_key('instance/attributes/canine_conf_preempt') == '+'
            },
            w
        )
    proc = subprocess.run(
        '/apps/google-cloud-sdk/bin/gcloud compute machine-types list --zones {}'.format(zone),
        shell=True,
        stdout=subprocess.PIPE
    )
    mtypes = pd.read_fwf(io.BytesIO(proc.stdout), index_col=0)
    with open('/apps/slurm/current/etc/instance_manifest.tsv', 'w') as manifest:
        manifest.write('hostname\tmachine_type\n')
        nodes = []
        partitions = {}
        for i, (name, row) in enumerate(mtypes.iterrows()):
            partname = '-'.join(name.split('-')[:-1])
            if len(name.split('-')) == 3:
                worker_name = '{}-worker[{}-{}]'.format(
                    cluster_name,
                    1+(i*n_workers),
                    (1+i)*n_workers
                )
                for j in range(1+(i*n_workers), 1+((1+i)*n_workers)):
                    manifest.write('{}-worker{}\t{}\n'.format(cluster_name, j, name))
                # NOTE: This weighting system may not be appropriate, but we can always revisit it later
                # Maybe weight should reflect cost per hour
                nodes.append('NodeName={name} CPUs={cpu} RealMemory={mem} State=CLOUD Weight={cpu}'.format(
                    name=worker_name,
                    cpu=row.CPUS,
                    mem=int((993 * row.MEMORY_GB)-400), # 993 is 97% of 1024. Accounts for system overhead in memory
                ))
                if partname in partitions:
                    partitions[partname].append(worker_name)
                else:
                    partitions[partname] = [worker_name]
                if gpu_count > 0:
                    gpu_name = '{}-xgpu-worker[{}-{}]'.format(
                        cluster_name,
                        1+(i*n_workers),
                        (1+i)*n_workers
                    )
                    nodes.append('NodeName={name} CPUs={cpu} RealMemory={mem} State=CLOUD Weight={weight} Gres=gpu:{gpu_type}:{gpu_count}'.format(
                        name=gpu_name,
                        cpu=row.CPUS,
                        mem=int((993 * row.MEMORY_GB)-400), # 993 is 97% of 1024. Accounts for system overhead in memory,
                        weight=2*row.CPUS, # weight GPU instances higher
                        gpu_type=gpu_type,
                        gpu_count=gpu_count
                    ))
                    partitions[partname].append(gpu_name)
    return nodes, [
        'PartitionName={} Nodes={}'.format(key, ','.join(value))
        for key, value in partitions.items()
    ] + ['PartitionName=all Nodes={} Default=YES'.format(','.join(node for nodes in partitions.values() for node in nodes))]

class Config(dict):
    def __init__(self, path):
        with open(path) as r:
            self.text = r.read()

        super().__init__({
            match.group(1): None
            for match in placeholder.finditer(self.text)
        })

    def dump(self):
        output = '' + self.text
        for key, val in self.items():
            if val is None:
                raise ValueError("Setting '{}' left blank".format(key))
            output = output.replace('<{}>'.format(key), val)
        return output

    def write(self, path):
        with open(path, 'w') as w:
            w.write(self.dump())

def main():
    with open('/apps/slurm/scripts/custom_worker_start.sh', 'w') as w:
        w.write(read_meta_key('instance/attributes/canine_conf_worker_start'))
    with open('/apps/slurm/scripts/custom_controller_start.sh', 'w') as w:
        w.write(read_meta_key('instance/attributes/canine_conf_controller_start'))
    subprocess.check_call('bash /apps/slurm/scripts/custom_controller_start.sh', shell=True)
    subprocess.check_call("chown slurm: /apps/slurm/scripts/*", shell=True, executable='/bin/bash')
    subprocess.check_call("chmod 755 /apps/slurm/scripts/*", shell=True, executable='/bin/bash')
    subprocess.check_call("chmod 666 /apps/slurm/scripts/wrapper.log /apps/slurm/scripts/suspend-resume.log", shell=True, executable='/bin/bash')
    # Todo: Fix conf load paths
    # Fix startp script and mount point dirs
    # Determine startup script propagation
    cluster_name = read_meta_key('instance/attributes/canine_conf_cluster_name')
    controller_name = read_meta_key('instance/name')
    print("Starting cluster", cluster_name, "with controller", controller_name)
    slurm_conf = Config(
        os.path.join(
            (os.path.dirname(__file__)),
            'conf-templates',
            'slurm.conf'
        )
    )
    slurm_conf['CONTROLLER HOSTNAME'] = controller_name
    slurm_conf['CLUSTER NAME'] = cluster_name
    node_defs, part_defs = build_node_defs(cluster_name)
    slurm_conf['NODE DEFS'] = '\n'.join(node_defs)
    slurm_conf['PART DEFS'] = '\n'.join(part_defs)
    slurm_conf.write('/apps/slurm/current/etc/slurm.conf')
    print("Saving slurm conf with", len(node_defs), "node types across", len(part_defs), "partitions")

    slurmdbd_conf = Config(
        os.path.join(
            (os.path.dirname(__file__)),
            'conf-templates',
            'slurmdbd.conf'
        )
    )
    slurmdbd_conf['CONTROLLER HOSTNAME'] = controller_name
    slurmdbd_conf.write("/apps/slurm/current/etc/slurmdbd.conf")

    print("Starting services")

    subprocess.check_call('systemctl start mariadb', shell=True)

    subprocess.check_call(['mysql', '-u', 'root', '-e',
        "create user slurm"])
    subprocess.check_call(['mysql', '-u', 'root', '-e',
        "grant all on slurm_acct_db.* TO slurm"])
    # This last one is allowed to fail
    # subprocess.call(['mysql', '-u', 'root', '-e',
    #     "grant all on slurm_acct_db.* TO 'slurm'@'{0}';".format(controller_name)])

    subprocess.check_call(shlex.split('systemctl start slurmdbd'))

    time.sleep(10)

    subprocess.check_call('/apps/slurm/current/bin/sacctmgr -i add cluster {}'.format(cluster_name), shell=True)
    subprocess.check_call('/apps/slurm/current/bin/sacctmgr -i add account slurm', shell=True)
    subprocess.check_call('/apps/slurm/current/bin/sacctmgr -i add user {} account=slurm'.format(
        read_meta_key('instance/attributes/canine_conf_user').replace('@', '_').replace('.', '_')
    ), shell=True)

    subprocess.check_call('systemctl start slurmctld', shell=True)

    print("Configuring NFS mounts")

    with open('/etc/exports', 'w') as w:
        w.write('/home  {}-*(rw,no_subtree_check,no_root_squash)\n'.format(cluster_name))
        w.write('/etc/munge {}-*(rw,no_subtree_check,no_root_squash)\n'.format(cluster_name))
        w.write('/apps {}-*(rw,no_subtree_check,no_root_squash)\n'.format(cluster_name))
        sec_disk = read_meta_key('instance/attributes/canine_conf_sec')
        if len(sec_disk) and sec_disk != '-':
            w.write('{} {}-*(rw,no_subtree_check,no_root_squash)\n'.format(
                sec_disk.strip(),
                cluster_name
            ))

    subprocess.check_call('systemctl start nfs-server', shell=True)
    time.sleep(10)
    subprocess.check_call('systemctl restart nfs-server', shell=True)

    print("Cluster startup complete")


# Add user to docker group on workers
if __name__ == '__main__':
    main()

import functools
import multiprocessing
import time

from tensorboardX import SummaryWriter

import optax
import ray

from enwik8_loader import TextLoader
from mesh_transformer import util
from mesh_transformer.TPU_cluster import TPUCluster
from mesh_transformer.transformer_shard import CausalTransformer
from ray_tpu import start_ray, get_connection, create_tpu, wait_til, delete_tpu

head_info = ray.init(dashboard_host="0.0.0.0")
address = head_info['redis_address']

tpu_name = "mesh-transformer-test-3"
gradient_accumulation_steps = 4
per_replica_batch = 16
tpus_per_replica = 8
tpu_size = 128

# delete_tpu(tpu_name, "europe-west4-a")
create_tpu(tpu_name, "europe-west4-a", f"v3-{tpu_size}", True)
assert wait_til(tpu_name, "europe-west4-a", {'state': 'READY', 'health': 'HEALTHY'})

conns = get_connection(tpu_name, "europe-west4-a")

with multiprocessing.Pool(processes=len(address)) as p:
    p.map(functools.partial(start_ray, address=address), conns)

train_dataset = TextLoader("data/enwik8",
                           batchsize=(gradient_accumulation_steps, per_replica_batch * tpu_size // tpus_per_replica),
                           sample_size=1024, length=90000000)

opt = optax.chain(
    optax.clip_by_global_norm(1),
    optax.scale_by_adam(eps=1e-4),
    optax.scale(-1),
    optax.scale_by_schedule(util.gpt3_schedule(1_000, 20_000, 1e-4, 1e-5))
)

model_fn = functools.partial(CausalTransformer, dim=4096, heads=32, layer_count=24, vocab=256, seq=1024, optimizer=opt)

t = TPUCluster((tpu_size//tpus_per_replica, tpus_per_replica), len(address), model_fn)

start = time.time()
t.train(train_dataset.get_samples())
print(f"Compiled in {time.time() - start:.06}s")

writer = SummaryWriter(flush_secs=5)

step = 0
while True:
    loss = t.train(train_dataset.get_samples())
    writer.add_scalar('train/loss', loss, step)

    step += 1

ray.shutdown()

"""Where everything lives. Defaults match the service names in docker-compose.yml."""
import os
from decimal import Decimal

HAPROXY_HOST = os.getenv("HAPROXY_HOST", "haproxy")
HAPROXY_STATS_URL = os.getenv("HAPROXY_STATS_URL", f"http://{HAPROXY_HOST}:7000/;csv")
COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://coordinator:8000")
# Added to the HAProxy ports below. 0 inside Docker; 500 when talking to another
# laptop's HAProxy from outside (it publishes 5000-5003 as 5500-5503).
HAPROXY_PORT_OFFSET = int(os.getenv("HAPROXY_PORT_OFFSET", "0"))

# Coordinator leader election (multi-laptop). Unset = a single coordinator that
# is always the leader (the original setup).
ELECTION_ETCD = os.getenv("ELECTION_ETCD")                       # e.g. http://10.60.1.152:2379
COORDINATOR_NAME = os.getenv("COORDINATOR_NAME", "coordinator")
COORDINATOR_ADVERTISE_URL = os.getenv("COORDINATOR_ADVERTISE_URL", COORDINATOR_URL)

# Where the coordinator's durable log lives. Unset = the standalone coord-db.
# "A" = a database inside Branch A's replicated cluster, so it survives the loss
# of any one laptop and every coordinator candidate can read it.
COORD_LOG_BRANCH = os.getenv("COORD_LOG_BRANCH")
COORD_LOG_DB = os.getenv("COORD_LOG_DB", "coordlog")

# "10.60.1.152=Christy 10.60.14.88=Yash" -> {"10.60.1.152": "Christy", ...}
LAPTOP_NAMES = dict(p.split("=", 1) for p in os.getenv("LAPTOP_NAMES", "").split() if "=" in p)

# The bank is sharded by branch. The first letter of an account id ("A-1001")
# is the shard key: it says which database cluster holds that account.
BRANCHES = {
    "A": {
        "name": "Branch A",
        "write_port": 5000 + HAPROXY_PORT_OFFSET,          # HAProxy -> current primary
        "read_port": 5001 + HAPROXY_PORT_OFFSET,           # HAProxy -> round-robin over replicas
        "nodes": ["pg-a1", "pg-a2", "pg-a3"],
        "haproxy_write": "branch_a_write",
        "haproxy_read": "branch_a_read",
    },
    "B": {
        "name": "Branch B",
        "write_port": 5002 + HAPROXY_PORT_OFFSET,
        "read_port": 5003 + HAPROXY_PORT_OFFSET,
        "nodes": ["pg-b1", "pg-b2", "pg-b3"],
        "haproxy_write": "branch_b_write",
        "haproxy_read": "branch_b_read",
    },
}

# Credentials (demo values; they also appear in patroni/patroni.yml).
PG_SUPERUSER = os.getenv("PG_SUPERUSER", "postgres")
PG_SUPERUSER_PASSWORD = os.getenv("PG_SUPERUSER_PASSWORD", "postgres-demo")
BANK_USER = "bank"
BANK_PASSWORD = os.getenv("BANK_PASSWORD", "bank-demo")
BANK_DB = "bank"

COORD_DB_DSN = os.getenv(
    "COORD_DB_DSN",
    "host=coord-db port=5432 dbname=coord user=coord password=coord-demo connect_timeout=3 client_encoding=utf8",
)

# Seed data: every branch gets a vault plus ten customer accounts.
# Deposits and withdrawals move money between a customer and the branch vault,
# so the total across the whole bank never changes. That constant is the
# invariant the dashboard checks.
VAULT_BALANCE = Decimal("1000000.00")
CUSTOMER_BALANCE = Decimal("10000.00")
CUSTOMERS = ["Aarav", "Diya", "Kabir", "Meera", "Rohan", "Ananya", "Vivaan", "Isha", "Arjun", "Tara"]
EXPECTED_TOTAL = len(BRANCHES) * (VAULT_BALANCE + CUSTOMER_BALANCE * len(CUSTOMERS))


# Where each database node can be reached.
# Single machine (docker-compose.yml): by container name, standard ports.
# Multi-machine (multi/): set PG_A1_IP=<laptop ip> etc.; the node is then at
# that IP on its own published ports (Postgres, Patroni REST API).
NODE_PORTS = {
    "pg-a1": (5411, 8011), "pg-a2": (5412, 8012), "pg-a3": (5413, 8013),
    "pg-b1": (5421, 8021), "pg-b2": (5422, 8022), "pg-b3": (5423, 8023),
}


def node_endpoint(node: str):
    """(host, postgres_port, patroni_port) for a node such as 'pg-a1'."""
    ip = os.getenv(node.upper().replace("-", "_") + "_IP")
    if ip:
        pg_port, api_port = NODE_PORTS[node]
        return ip, pg_port, api_port
    return node, 5432, 8008


def branch_of(account_id: str) -> str:
    """Routing rule: 'A-1001' lives in branch A, 'B-1003' in branch B."""
    prefix = account_id.split("-", 1)[0].upper()
    if prefix not in BRANCHES:
        raise ValueError(f"unknown branch for account {account_id!r}")
    return prefix


def vault_of(branch: str) -> str:
    return f"{branch}-VAULT"

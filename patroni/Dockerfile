# PostgreSQL 16 + Patroni (the high-availability manager that handles
# replication setup, leader election and automatic failover).
FROM postgres:16

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv curl \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/patroni \
 && /opt/patroni/bin/pip install --no-cache-dir "patroni[etcd3]==4.1.5" "psycopg2-binary>=2.9" \
 && ln -s /opt/patroni/bin/patroni /usr/local/bin/patroni \
 && ln -s /opt/patroni/bin/patronictl /usr/local/bin/patronictl

COPY patroni.yml /etc/patroni/patroni.yml

# Lets you run `patronictl list` inside a container without extra flags.
ENV PATRONICTL_CONFIG_FILE=/etc/patroni/patroni.yml

USER postgres
EXPOSE 5432 8008

# Replaces the stock image entrypoint: Patroni (not the image) runs initdb,
# takes base backups for replicas, and starts/stops PostgreSQL.
ENTRYPOINT ["patroni", "/etc/patroni/patroni.yml"]

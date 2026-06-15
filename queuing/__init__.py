"""queuing — SQL-backed named FIFO queues that share the caller's database
and transactions.

Public surface:

    from queuing import (
        Queue,
        SCHEMA_DDL,
        install_schema,
        queue_names,
        queue_depths,
    )

See README.md for usage; queue.py for the implementation.
"""
from .queue import (
    SCHEMA_DDL,
    Queue,
    install_schema,
    queue_depths,
    queue_names,
)

__all__ = [
    "SCHEMA_DDL",
    "Queue",
    "install_schema",
    "queue_depths",
    "queue_names",
]

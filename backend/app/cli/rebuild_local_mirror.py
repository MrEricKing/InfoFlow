from __future__ import annotations

from app.db.session import get_session_factory
from app.services.knowledge_mirror import rebuild_local_mirror_from_db


def main() -> None:
    with get_session_factory()() as session:
        batch = rebuild_local_mirror_from_db(session)
        session.commit()
        print(
            f"Local mirror rebuild finished: batch_id={batch.id} "
            f"status={batch.status} completed={batch.completed_items} failed={batch.failed_items}"
        )


if __name__ == "__main__":
    main()

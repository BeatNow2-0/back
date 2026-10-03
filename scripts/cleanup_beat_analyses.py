"""One-shot cleanup for expired unpublished beat analyses; suitable for cron."""
import asyncio

from routes.beat_analysis_routes import cleanup_expired_beat_analyses


async def main() -> None:
    removed = await cleanup_expired_beat_analyses()
    print(f"Expired beat analyses cleaned: {removed}")


if __name__ == "__main__":
    asyncio.run(main())

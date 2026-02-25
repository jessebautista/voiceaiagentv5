import os
import logging
from typing import Optional
from temporalio.client import Client

logger = logging.getLogger("app.temporal_client")

_client: Optional[Client] = None

async def init_temporal_client() -> Client:
    """Initialize and connect the global Temporal client."""
    global _client
    if _client:
        return _client
    temporal_url = os.getenv("TEMPORAL_URL", "localhost:7233")
    try:
        logger.info(f"Connecting to Temporal Server at {temporal_url}...")
        _client = await Client.connect(temporal_url)
        logger.info("Successfully connected to Temporal!")
        return _client
    except Exception as e:
        logger.error(f"Failed to connect to Temporal API: {e}")
        raise e

def get_temporal_client() -> Optional[Client]:
    """Get the initialized Temporal client."""
    return _client

"""Load secrets from Google Cloud Secret Manager, with env var fallback for local dev."""

import os
import logging

logger = logging.getLogger(__name__)

_cache = {}


def get_secret(name: str) -> str:
    """
    Get a secret value. Tries Secret Manager first, falls back to env vars.

    Args:
        name: Secret name (e.g., "GEMINI_API_KEY")

    Returns:
        The secret value string
    """
    if name in _cache:
        return _cache[name]

    # Try Google Cloud Secret Manager first
    project_id = os.environ.get("GCP_PROJECT_ID")
    if project_id:
        try:
            from google.cloud import secretmanager

            client = secretmanager.SecretManagerServiceClient()
            secret_path = f"projects/{project_id}/secrets/{name}/versions/latest"
            response = client.access_secret_version(request={"name": secret_path})
            value = response.payload.data.decode("UTF-8")
            _cache[name] = value
            logger.info(f"Loaded secret '{name}' from Secret Manager")
            return value
        except Exception as e:
            logger.warning(f"Secret Manager failed for '{name}': {e}, falling back to env var")

    # Fallback to environment variable
    value = os.environ.get(name)
    if value:
        _cache[name] = value
        return value

    raise ValueError(f"Secret '{name}' not found in Secret Manager or environment variables")

"""Test package setup.

The feature-flag reader (app/db/feature_flags.py) would otherwise try to
reach Firestore on every save/preview under test. Point it at an empty
in-memory `featureFlags` collection so every flag reads OFF and no test ever
waits on Google credentials. Tests that need a flag ON patch
`app.services.save._use_v2` (or `feature_flags.get_fs_client`) themselves.
"""

import os

for _key, _value in {
    "GOOGLE_MAPS_API_KEY": "test-key",
    "BQ_PROJECT": "test-project",
    "BQ_DATASET": "test_dataset",
    "BQ_TABLE": "test_table",
    "BQ_STRUCTURED_TABLE": "test_structured_table",
    "FIRESTORE_PROJECT": "test-project",
}.items():
    os.environ.setdefault(_key, _value)


class _EmptyCollection:
    def stream(self):
        return iter(())


class _NoFlagsClient:
    def collection(self, _name):
        return _EmptyCollection()


def _install_offline_flags():
    from app.db import feature_flags

    feature_flags.get_fs_client = lambda: _NoFlagsClient()
    feature_flags.reset_cache()


_install_offline_flags()

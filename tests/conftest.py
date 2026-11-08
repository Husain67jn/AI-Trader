"""Test environment. Every test runs against a throwaway database, never the project's.

`app/config.py` reads DATA_DIR and DB_PATH once, when it is imported, so they have to be
set here before the first import of `app.anything` rather than in a fixture.
"""
import atexit
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="ati-tests-")
os.environ["TRADER_DATA_DIR"] = _TMP
os.environ["TRADER_DB"] = os.path.join(_TMP, "test.db")
os.environ["TRADER_OWNER_PASSWORD"] = "test-password-123"
os.environ["TRADER_BROKER"] = "mock"
os.environ["TRADER_SCHEDULER"] = "0"
os.environ["TRADER_LLM_PROVIDER"] = "mock"

# One run, one directory: without this the temp folder collects a database per pytest
# invocation and never gives it back.
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

# SPDX-License-Identifier: Apache-2.0
"""Exercise Windows process ownership with real disposable child processes."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("windows_server", REPO / "scripts/windows-server.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@unittest.skipUnless(os.name == "nt", "native Windows process handles")
class OwnershipTests(unittest.TestCase):
    def temporary(self):
        directory = REPO / "build-windows-cpu/service-validation/test-tmp"
        directory.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(dir=directory)

    def child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        def reap():
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
        self.addCleanup(reap)
        return child

    def test_job_close_terminates_child(self):
        child = self.child()
        job = launcher.Job()
        try:
            job.assign(child)
        finally:
            job.close()
        self.assertIsNotNone(child.wait(timeout=10))

    def test_supervisor_crash_closes_job_and_terminates_server(self):
        with self.temporary() as temporary:
            receipt = Path(temporary) / "pid"
            code = (
                "import importlib.util,subprocess,sys,time;from pathlib import Path;"
                "s=importlib.util.spec_from_file_location('launcher',sys.argv[1]);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);j=m.Job();"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],creationflags=subprocess.CREATE_NO_WINDOW);"
                "j.assign(p);Path(sys.argv[2]).write_text(str(p.pid));time.sleep(60)"
            )
            parent = subprocess.Popen([sys.executable, "-c", code, str(REPO / "scripts/windows-server.py"), str(receipt)],
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 10
                while not receipt.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(receipt.exists())
                child = launcher.ProcessRef(int(receipt.read_text()))
                try:
                    self.assertTrue(child.alive())
                    parent.terminate()
                    parent.wait(timeout=10)
                    self.assertEqual(child.kernel.WaitForSingleObject(child.handle, 10000), 0)
                finally:
                    child.terminate()
                    child.close()
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=10)

    def test_creation_time_mismatch_never_targets_reused_pid(self):
        child = self.child()
        actual = launcher.ProcessRef(child.pid)
        try:
            wrong = launcher.ProcessRef(child.pid, actual.created + 1)
            try:
                self.assertFalse(wrong.alive())
                wrong.terminate()
                self.assertIsNone(child.poll())
            finally:
                wrong.close()
        finally:
            actual.close()
        child.terminate()
        child.wait(timeout=10)

    def test_stop_token_from_previous_run_is_ignored(self):
        with self.temporary() as temporary:
            run = Path(temporary)
            launcher.save(run / "stop.json", {"token": "previous"})
            self.assertFalse(launcher.stop_requested(run, "current"))


if __name__ == "__main__":
    unittest.main()

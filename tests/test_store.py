from unittest import TestCase
import tempfile
from concurrent.futures import ThreadPoolExecutor
import threading

from nethack_harness.store import Store


class StoreTest(TestCase):
    def test_simultaneous_clients_create_one_usable_database(self):
        with tempfile.TemporaryDirectory() as directory:
            barrier = threading.Barrier(8)

            def worker(index):
                barrier.wait(timeout=5)
                store = Store(directory)
                try:
                    store.write(str(index), index)
                finally:
                    store.close()

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(worker, range(8)))
            store = Store(directory)
            try:
                self.assertEqual([store.read(str(n)) for n in range(8)], list(range(8)))
            finally:
                store.close()

    def test_acknowledging_command_preserves_concurrent_enqueue(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                first = store.enqueue("pause")
                barrier = threading.Barrier(2)

                def worker(ack):
                    connection = Store(directory)
                    try:
                        barrier.wait(timeout=5)
                        return connection.acknowledge(first) if ack else connection.enqueue("stop")
                    finally:
                        connection.close()

                with ThreadPoolExecutor(max_workers=2) as pool:
                    ack, enqueue = pool.submit(worker, True), pool.submit(worker, False)
                    ack.result(timeout=10)
                    second = enqueue.result(timeout=10)
                self.assertEqual(store.pending(), [(second, {"command": "stop"})])
                store.acknowledge(second)
                self.assertEqual(store.pending(), [])
            finally:
                store.close()

    def test_unfinished_input_is_visible_in_export(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            number = store.begin("engine", {"state": {"observation": {"phase": "play"}}})
            store.choice(number, "wait:1", {"answers": {"action": {"choice": "wait:1"}}}, 0.1)
            store.input(number, ".", "action")
            store.close()
            store = Store(directory)
            try:
                record, = store.records()
                self.assertIsNone(record["outcome"])
                self.assertEqual(record["inputs"][0]["status"], "pending")
            finally:
                store.close()

    def test_replies_are_consumed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                store.reply(1, {"ok": True, "value": "paused"})
                self.assertEqual(store.take_reply(1), {"ok": True, "value": "paused"})
                self.assertIsNone(store.take_reply(1))
            finally:
                store.close()

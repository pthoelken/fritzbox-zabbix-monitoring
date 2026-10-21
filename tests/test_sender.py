import subprocess
import unittest
from unittest.mock import patch

from src import fritzbox_monitor as monitor


class SenderFailureTests(unittest.TestCase):
    def test_failed_batch_is_visible_after_a_successful_batch(self):
        result = subprocess.CompletedProcess(
            [], 2,
            "processed: 250; failed: 0; total: 250\n"
            "processed: 0; failed: 10; total: 10\n",
            "",
        )
        with self.assertLogs(monitor.log, level="ERROR") as captured:
            monitor._log_sender_failure(result, "Zabbix sender")
        self.assertIn("failed: 10", "\n".join(captured.output))

    def test_rejected_values_report_server_summary_and_configuration_hint(self):
        result = subprocess.CompletedProcess(
            [], 2,
            'Response: "processed: 0; failed: 155; total: 155; seconds spent: 0.001429"\n'
            'sent: 155; skipped: 0; total: 155\n',
            'DEBUG: sending private-payload\n',
        )
        for send, values in [
            (monitor.send_to_zabbix, {"fritzbox.collector.status": 1}),
            (monitor.send_log_events_to_zabbix, [{"clock": 1, "message": "event"}]),
        ]:
            with self.subTest(sender=send.__name__), \
                    patch.object(monitor, "ZABBIX_SERVER", "127.0.0.1"), \
                    patch.object(monitor, "FRITZBOX_HOSTNAME", "router.test"), \
                    patch.object(monitor.subprocess, "run", return_value=result), \
                    self.assertLogs(monitor.log, level="ERROR") as captured:
                self.assertFalse(send(values))
            messages = "\n".join(captured.output)
            self.assertIn("processed: 0; failed: 155; total: 155", messages)
            self.assertIn("router.test", messages)
            self.assertIn("Allowed hosts", messages)
            self.assertIn("server/proxy log", messages)
            self.assertNotIn("private-payload", messages)


if __name__ == "__main__":
    unittest.main()

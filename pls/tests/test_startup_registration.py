import subprocess
import sys


def test_fresh_process_registers_command_consumer():
    result = subprocess.run(
        [sys.executable, '-c', (
            'import json; from pls.main import app; '
            'from faststream.specification import AsyncAPI; '
            'print(AsyncAPI(app.broker).to_specification().to_json())'
        )],
        check=True, capture_output=True, text=True, timeout=30,
    )
    assert 'payment.lightning.commands' in result.stdout
    assert 'HandlePaymentCommands' in result.stdout

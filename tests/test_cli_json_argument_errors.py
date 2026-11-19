import json

import pytest

from deskpilot import cli


@pytest.mark.parametrize(
    "arguments",
    [["click"], ["wait-for", "0xabc", "--timeout", "oops"], ["windows", "--unknown"]],
)
def test_json_argument_errors_have_an_invalid_code(arguments, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--json", *arguments])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["code"] == "invalid"
    assert payload["message"]

"""Error signatures of ``compress.base``: frame heads match on what they say, not their path."""

from __future__ import annotations

import pytest

from token_sparen.compress import is_error_line
from token_sparen.compress.base import error_line_indices

FRAME_HEADS = [
    '  File "/usr/lib/python3.12/site-packages/requests/exceptions.py", line 12, in check',
    '  File "/srv/app/errors/handlers.py", line 40, in dispatch',
    "    at Object.handle (/app/node_modules/express/lib/error-handler.js:10:5)",
    "    at /app/node_modules/http-errors/index.js:30:12",
    "\tat com.example.Service.run(Exceptions.java:42)",
]


@pytest.mark.parametrize("line", FRAME_HEADS)
def test_frame_head_with_exception_in_path_is_not_error_line(line: str) -> None:
    assert not is_error_line(line)
    assert error_line_indices(f"ok\n{line}\nok") == []


@pytest.mark.parametrize(
    "line",
    [
        '  File "/srv/app/errors.py", line 3, in <module>  # ValueError raised here',
        "    at new ValidationError (/app/src/errors.js:4:11)",
        "    at Error.captureStackTrace (/app/src/errors.js:1:1)",
        "ValueError: bad input in /srv/app/errors.py",
        "error: /srv/app/exceptions.py not found",
    ],
)
def test_error_words_outside_the_frame_path_still_count(line: str) -> None:
    assert is_error_line(line)
    assert error_line_indices(f"ok\n{line}\nok") == [1]

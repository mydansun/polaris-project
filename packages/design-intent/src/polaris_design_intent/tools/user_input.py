from __future__ import annotations

from typing import Protocol


class ClarificationQuestion(dict):
    """Flat shape compatible with the existing Polaris clarification SSE event.

    Keys:
      id: str            — stable handle used by the frontend card
      title: str         — the question shown to the user
      description: str?  — optional helper text
      choices: list[str]? — optional pick-list; free-text always allowed
      required: bool     — defaults to True
    """


class ClarificationAnswer(dict):
    """Keys:
      question_id: str
      answer: str
    """


class UserInputFn(Protocol):
    """Async callable the graph uses to surface questions to the user and
    wait for answers.  The worker supplies an implementation that bridges
    to the existing clarification SSE + Redis pubsub pipeline."""

    async def __call__(
        self, questions: list[ClarificationQuestion]
    ) -> list[ClarificationAnswer]: ...

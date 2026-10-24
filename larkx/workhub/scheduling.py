"""Model admission with foreground priority and exclusive conversation updates."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(eq=False)
class _Ticket:
    scheduler: object
    ready: asyncio.Future
    background: bool
    chat_id: str
    granted: bool = False
    model_held: bool = False

    def release_model(self):
        # Keep the conversation reserved while validating/persisting/sending,
        # but allow other conversations to use the model immediately.
        self.scheduler.release_model(self)


class ModelScheduler:
    def __init__(self, limit=2, background_limit=1):
        if not 0 < background_limit <= limit:
            raise ValueError("Invalid model concurrency limits")
        self.limit = limit
        self.background_limit = background_limit
        self.active = 0
        self.active_background = 0
        self.busy_chats = set()
        self.waiters = []

    @property
    def queued(self):
        return sum(not ticket.ready.cancelled() for ticket in self.waiters)

    def configure(self, limit, background_limit):
        if not 0 < background_limit <= limit:
            raise ValueError("Invalid model concurrency limits")
        self.limit, self.background_limit = limit, background_limit
        self._dispatch()

    def _dispatch(self):
        while self.active < self.limit:
            candidate = None
            for background in (False, True):
                for ticket in self.waiters:
                    if ticket.ready.cancelled() or ticket.background != background:
                        continue
                    if ticket.chat_id and ticket.chat_id in self.busy_chats:
                        continue
                    if background and self.active_background >= self.background_limit:
                        continue
                    candidate = ticket
                    break
                if candidate is not None:
                    break
            if candidate is None:
                return
            self.waiters.remove(candidate)
            candidate.granted = candidate.model_held = True
            self.active += 1
            self.active_background += int(candidate.background)
            if candidate.chat_id:
                self.busy_chats.add(candidate.chat_id)
            candidate.ready.set_result(None)

    def release_model(self, ticket):
        if ticket.model_held:
            ticket.model_held = False
            self.active -= 1
            self.active_background -= int(ticket.background)
        self._dispatch()

    @asynccontextmanager
    async def slot(self, *, background=False, chat_id=""):
        ticket = _Ticket(
            self, asyncio.get_running_loop().create_future(), background, chat_id
        )
        self.waiters.append(ticket)
        self._dispatch()
        try:
            await ticket.ready
            yield ticket
        finally:
            if ticket in self.waiters:
                self.waiters.remove(ticket)
            if ticket.granted and ticket.chat_id:
                self.busy_chats.discard(ticket.chat_id)
            self.release_model(ticket)

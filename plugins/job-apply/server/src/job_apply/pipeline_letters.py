"""Cover letters, for Applier (a mixin): with the desk's "Write a cover letter for each job" on (Applier.letters), each
job's is written in the background as soon as it's queued (letters.write_letter, through the person's own Claude Code
sign-in), a few at a time (LETTERS_AT_ONCE), and goes into its folder (render_document), where autofill uploads it to a
cover letter box. A cover letter box reached while the job's letter is still being written waits for it a while
(LETTER_WAIT), then goes on without. A letter already in the folder is used as it is: never written again on a retry or
a Carry on.

The first letter ever written waits on its job's card (need "cover_letter") for the person's OK (Use it), kept in the
folder meanwhile as a draft no upload takes (letters.DRAFT); once they've said so (Applier.letters_approved, kept in
desk.json), each letter goes in without asking. Their Turn cover letters off turns the switch off there. Until the OK,
one letter at a time: none other is written while one is being written or its draft waits (the drafts in the jobs'
folders say so, a restart of the desk too: _first_letter_out).

Whatever goes wrong, the job carries on with the profile's usual cover letter (documents.cover_letter), or none: never
held for ever. What stops every letter (no claude command, not signed in, ...) is said once on the desk
(Applier.letters_problem), and tried again LETTER_RETRY seconds later or when the switch is turned off and on; what
stops one job's is said in its log. Letters refused for what they claim LETTERS_REFUSED_MOST times in a row (the desk's
check, or Claude's: a check that flags every letter would cost two calls a job for nothing) stop them all, said once on
the desk, until the switch is turned off and on or the desk restarts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config, letters
from .autofill import clean_label, norm, tailored_document
from .postings import fetch_posting

if TYPE_CHECKING:
    from .pipeline import Run

# (a letter is two calls to Claude: its writing, then the check of what it claims, letters.check_letter)
LETTER_WAIT = 120.0  # seconds a cover letter box waits for the job's letter, still being written or checked
LETTER_FIRST_WAIT = 180.0  # and the first letter's job, before its tab opens, for the letter it waits on
LETTERS_AT_ONCE = 2  # letters written (and checked) at the same time
LETTER_RETRY = 600.0  # seconds after what stops every letter before the desk tries again
LETTERS_REFUSED_MOST = 4  # letters refused for what they claim, in a row, before the desk stops writing them
POSTING_LEAST = 200  # characters of a posting's text under which it's read again from its address first

LETTER_SAYS = ("This is the first cover letter the desk wrote for you (with your Claude login), and this job waits for "
               "your say. If it's right, press Use it: from then on each job's letter goes in without asking. To "
               "change how they're written, tell Claude in the chat what to change, or press Turn cover letters off.")


def has_letter(job: dict[str, Any]) -> bool:
    """A cover letter for this job in its folder, ready to upload (render_document's, or the person's own)."""
    return tailored_document(job, "cover_letter") is not None


def draft_of(job: dict[str, Any]) -> Path | None:
    return Path(job["folder"]) / letters.DRAFT if job.get("folder") else None


def cover_box(data: dict[str, Any]) -> bool:
    """A file box for a cover letter on the page (by its words or its section's, as autofill picks the file)."""
    return any(f.get("kind") == "file" and re.search(r"cover", f"{norm(clean_label(f.get('label') or ''))} "
                                                               f"{norm(f.get('section'))}")
               for f in data.get("fields") or [] if isinstance(f, dict))


class LettersMixin:
    """Applier's cover letters."""

    if TYPE_CHECKING:  # (the applier's own)
        srv: Any
        runs: dict[int, Run]
        tasks: deque[tuple[str, int]]
        pressed: set[int]
        _wake: asyncio.Event
        letters: bool
        letters_approved: bool
        letters_problem: str | None
        _letters_problem_at: float
        _letter_tasks: dict[int, asyncio.Task]
        _letter_slots: asyncio.Semaphore | None
        _draft_ids: set[int] | None
        _letters_retried_at: float
        _letters_refused: int
        _letters_tripped: bool

        def _log(self, run: Run, text: str) -> None: ...

        def _pause(self, run: Run, need: str, reason: str, questions: list[dict[str, Any]] | None = None,
                   seen: dict[str, Any] | None = None, *, code: str) -> None: ...

        def enqueue(self, job_id: int, submit: bool = False, front: bool = False) -> Run: ...

        def go_on_for(self, run: Run) -> None: ...

        def _held_back(self, task: tuple[str, int]) -> bool: ...

    # ------------------------------------------------- the switch and the card
    def set_letters(self, on: bool) -> None:
        """The desk's "Write a cover letter for each job". Off: the letters being written are dropped, and so is a first
        letter waiting for the person's say (its job goes on without it). On (again): what stopped every letter is
        tried again."""
        self.letters, self.letters_problem = on, None
        self._letters_refused, self._letters_tripped = 0, False
        if not on:
            for task in list(self._letter_tasks.values()):
                task.cancel()
            for job_id in list(self._drafts()):
                self._drop_draft(job_id)
            for run in [r for r in self.runs.values() if r.letter_text]:
                run.letter_text = ""
            for run in [r for r in self.runs.values() if r.status == "needs_you" and r.need == "cover_letter"]:
                with contextlib.suppress(KeyError, ValueError):
                    self.enqueue(run.job_id, submit=run.submit)
        else:
            self._letters_next()

    def approve_letter(self, job_id: int) -> Run:
        """The person's Use it on the first letter's card: from now on each letter goes in without asking. This job's
        goes into its folder (the draft, unless Claude has put one there for it since, at the person's word: in the
        background, as a letter being written, which its cover letter box waits for) and the job goes on at once, and
        the queued jobs' letters are written now."""
        run = self._letter_card(job_id)
        self.letters_approved = True
        job = self.srv.tracker().get(job_id, with_description=False) or {}
        draft = draft_of(job)
        text = draft.read_text(encoding="utf-8") if draft is not None and draft.is_file() else run.letter_text
        self._drop_draft(job_id)
        run.letter_text = ""
        self._log(run, "Use it: you said the cover letter is right, so each job's goes in without asking from now on")
        if text and not has_letter(job):
            self._letter_tasks[job_id] = asyncio.get_running_loop().create_task(self._put_in(run, text))
        run = self.enqueue(job_id, submit=run.submit, front=True)
        self.go_on_for(run)
        self.pressed.add(job_id)  # (the person's own press: it goes even while the queue is paused)
        self._letters_next()
        return run

    async def _put_in(self, run: Run, text: str) -> None:
        try:
            await self._letter_into_folder(run, text)
        finally:
            if self._letter_tasks.get(run.job_id) is asyncio.current_task():
                del self._letter_tasks[run.job_id]
            self._wake.set()

    def letters_off_for(self, job_id: int) -> Run:
        """The person's Turn cover letters off on the first letter's card: the switch goes off (no letter is written
        for any job), this letter is dropped, and the job goes on with the usual cover letter, if any."""
        run = self._letter_card(job_id)
        self._log(run, "you turned cover letters off: this job goes on with your usual cover letter, if you have one")
        self.set_letters(False)  # (which drops its letter, and queues the job again)
        self.go_on_for(run)
        self.pressed.add(job_id)
        return run

    def letter_rewritten(self, job_id: int, markdown: str) -> None:
        """Claude wrote this job's cover letter again (render_document, which put it in the job's draft while the
        letter waits for the person's say), at the person's word: the first letter's card shows the new one, which Use
        it renders and puts in."""
        run = self.runs.get(job_id)
        if run is not None and run.status == "needs_you" and run.need == "cover_letter":
            run.letter_text = markdown
            run.updated = time.time()

    def letter_waits(self, job: dict[str, Any]) -> bool:
        """Does this job's first letter wait for the person's say (its draft in its folder, the OK not given)? Then a
        letter written for it goes into the draft, never into the folder as one to upload (render_document)."""
        draft = draft_of(job)
        return not self.letters_approved and draft is not None and draft.is_file()

    def _letter_dropped(self, job_id: int) -> None:
        """The job is skipped or marked applied: its letter being written is dropped, and so is its first letter
        waiting for the person's say (there's no card left to say it on). Before the OK, the next job's is written."""
        if (task := self._letter_tasks.get(job_id)) is not None:
            task.cancel()
        if job_id in self._drafts() or (run := self.runs.get(job_id)) is not None and run.letter_text:
            self._drop_draft(job_id)
        if (run := self.runs.get(job_id)) is not None:
            run.letter_text = ""
        if self.letters and not self.letters_approved:
            self._letters_next()

    def _letter_card(self, job_id: int) -> Run:
        run = self.runs.get(job_id)
        if run is None or run.status != "needs_you" or run.need != "cover_letter":
            raise ValueError("That job isn't waiting on its cover letter any more.")
        return run

    # ------------------------------------------------- writing in the background
    def _letters_next(self) -> None:
        """Each job under way, then each in the queue in its order, then each waiting on the person, gets its letter
        written (_letter_soon says which): the ones nearest a cover letter box first."""
        running = [r.job_id for r in self.runs.values() if r.status == "running"]
        waiting = [job_id for kind, job_id in list(self.tasks) if kind == "apply"]
        paused = [r.job_id for r in self.runs.values() if r.status in ("queued", "needs_you")]
        for job_id in dict.fromkeys(running + waiting + paused):
            self._letter_soon(job_id)

    def _letters_retry(self) -> None:
        """What stopped every letter, tried again LETTER_RETRY seconds on (the desk's notice says so): once each time
        it's said. (Between jobs: Applier._tick.)"""
        if (self.letters and self.letters_problem is not None and not self._letters_tripped
                and time.monotonic() - self._letters_problem_at >= LETTER_RETRY
                and self._letters_retried_at != self._letters_problem_at):
            self._letters_retried_at = self._letters_problem_at
            self._letters_next()

    def _first_letter_out(self, job_id: int) -> bool:
        """Before the person's OK, one letter at a time: another job's being written, or written and waiting for their
        say, its draft in its folder (on its card, or to be shown there when its job's turn comes: after a restart of
        the desk too). Not a draft whose job is finished (applied to, or skipped): nothing waits on it."""
        from .pipeline import FINISHED

        if any(j != job_id and not t.done() for j, t in self._letter_tasks.items()):
            return True
        for other in sorted(self._drafts() - {job_id}):
            job = self.srv.tracker().get(other, with_description=False)
            draft = draft_of(job) if job else None
            if draft is None or not draft.is_file():
                self._drafts().discard(other)  # (gone since: the job's, or its folder, removed)
            elif job is not None and has_letter(job):
                self._drop_draft(other)  # (a letter in its folder is the one that goes: the person's own)
            elif job is not None and job.get("status") not in (*FINISHED, "skipped"):
                return True
        return False

    def _drafts(self) -> set[int]:
        """The jobs whose first letter waits for the person's say in their folder (letters.DRAFT): read from the jobs'
        folders the first time (what a restart of the desk keeps), then kept as drafts are written and dropped."""
        if self._draft_ids is None:
            found: set[int] = set()
            with contextlib.suppress(OSError):
                for path in config.applications_dir().glob(f"*/{letters.DRAFT}"):
                    if m := re.match(r"(\d+)-", path.parent.name):
                        found.add(int(m.group(1)))
            self._draft_ids = found
        return self._draft_ids

    def _drop_draft(self, job_id: int) -> None:
        job = self.srv.tracker().get(job_id, with_description=False) or {}
        if (draft := draft_of(job)) is not None:
            with contextlib.suppress(OSError):
                draft.unlink(missing_ok=True)
        self._drafts().discard(job_id)

    def _letter_soon(self, job_id: int) -> None:
        """Start writing this job's letter in the background (the switch on): unless one is in its folder (or waits
        there for the person's OK), is being written, or was tried for it already; not while what stops every letter
        stands (LETTER_RETRY); and before the person's OK, not while another job's first letter is out."""
        run = self.runs.get(job_id)
        if run is not None and not run.letter:
            self._note_letter(run)  # (one in its folder already, from before a restart: its card links it)
        if not self.letters:
            return
        if (task := self._letter_tasks.get(job_id)) is not None and not task.done():
            return
        if self._letters_stopped():
            return
        if run is None or run.letter_tried or run.status in ("submitted", "skipped", "ready"):
            return
        if self._held_back(("apply", job_id)):  # (back after a restart, waiting for Carry on with the queue)
            return
        # (before the OK, only for a job yet to begin, which waits for it before its tab opens; and one at a time)
        if not self.letters_approved and (run.status != "queued" or self._first_letter_out(job_id)):
            return
        job = self.srv.tracker().get(job_id, with_description=False)
        if job is None or has_letter(job) or ((draft := draft_of(job)) is not None and draft.is_file()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # (queued from outside the desk's loop: no letter)
            return
        self._letter_tasks[job_id] = loop.create_task(self._write_letter(job_id))

    def _letters_stopped(self) -> bool:
        """What stops every letter stands (said on the desk), and isn't LETTER_RETRY seconds old yet; or the letters
        kept failing their check (until the switch is turned off and on)."""
        return self._letters_tripped or (self.letters_problem is not None
                                         and time.monotonic() - self._letters_problem_at < LETTER_RETRY)

    def _note_letter(self, run: Run) -> None:
        """The cover letter (a PDF) in the job's folder, for its card's link."""
        job = self.srv.tracker().get(run.job_id, with_description=False) or {}
        found = tailored_document(job, "cover_letter") if job.get("folder") else None
        if found is not None and Path(found).suffix.lower() == ".pdf":
            run.letter = Path(found).name

    async def _write_letter(self, job_id: int) -> None:
        if self._letter_slots is None:
            self._letter_slots = asyncio.Semaphore(LETTERS_AT_ONCE)
        try:
            async with self._letter_slots:
                await self._write_letter_now(job_id)
        except asyncio.CancelledError:
            if (run := self.runs.get(job_id)) is not None and not run.letter:
                run.letter_tried = False  # (dropped, not tried: written when it's queued again)
            raise
        except Exception:  # (whatever goes wrong, the job goes on without one)
            logging.getLogger(__name__).warning("couldn't write job %s's cover letter", job_id, exc_info=True)
        finally:
            if self._letter_tasks.get(job_id) is asyncio.current_task():
                del self._letter_tasks[job_id]
            if self.letters and not self.letters_approved:
                self._letters_next()  # (the first letter not written: the next job's turn)
            self._wake.set()

    async def _write_letter_now(self, job_id: int) -> None:
        run = self.runs.get(job_id)
        job = self.srv.tracker().get(job_id)
        if run is None or job is None or has_letter(job) or run.status in ("skipped", "submitted", "ready"):
            return  # (its job done meanwhile, while it waited for its turn: none written)
        if self._letters_stopped() or not self.letters:
            return  # (another letter met what stops them all, while this one waited: tried again with them)
        run.letter_tried = True  # (once a job: a retry or a Carry on never writes it again)
        description = str(job.get("description") or "")
        if len(description.strip()) < POSTING_LEAST and job.get("url"):
            with contextlib.suppress(Exception):  # (not read: the job's own text, as short as it is)
                read = (await fetch_posting(str(job["url"]))).description
                description = read if len(read) > len(description) else description
        if not description.strip():
            self._log(run, "no cover letter written for this job: its posting has no text to write it from, so your "
                           "usual cover letter goes in, if you have one")
            return
        try:
            letter = await letters.write_letter(job, config.Profile.load(), description)
        except letters.Unavailable as e:
            run.letter_tried = False  # (not this job's doing: its letter is written once that's fixed)
            if not self._letters_tripped:  # (the letters stopped meanwhile: that notice stays, KEPT_FAILING)
                self.letters_problem, self._letters_problem_at = str(e), time.monotonic()
            return
        except letters.LetterError as e:
            self._log(run, f"no cover letter written for this job: {e}, so your usual cover letter goes in, if you "
                           "have one")
            if isinstance(e, letters.Refused):
                self._letters_refused += 1
                if self._letters_refused >= LETTERS_REFUSED_MOST and not self._letters_tripped:
                    self._letters_tripped = True
                    self.letters_problem, self._letters_problem_at = letters.KEPT_FAILING, time.monotonic()
            return
        if not self._letters_tripped:  # (one that passed while the others kept failing is used; none more is written)
            self._letters_refused, self.letters_problem = 0, None
        if not self.letters or run.status in ("skipped", "submitted"):  # (turned off, or its job done, meanwhile)
            return
        if self.letters_approved:
            await self._letter_into_folder(run, letter.markdown)
            return
        draft = draft_of(job)
        if draft is None:
            return
        if not (run.status == "queued" or run.letter_wait):  # (its job went on without it: the next job's is written)
            self._log(run, "its cover letter, the first one, came after the job had gone on, so it isn't used")
            return
        draft.parent.mkdir(parents=True, exist_ok=True)
        config.write_text_atomic(draft, letter.markdown)
        self._drafts().add(job_id)
        run.letter_text = letter.markdown
        self._log(run, f"wrote the first cover letter for this job ({letter.by}): it waits for your say on its card")

    async def _letter_into_folder(self, run: Run, markdown: str) -> bool:
        """The letter as a PDF in the job's folder (render_document), where autofill uploads it. False where it isn't
        (said in the log: the usual cover letter goes instead)."""
        try:
            out = await self.srv.render_document("cover_letter", markdown, run.job_id)
        except Exception as e:
            out = {"error": str(e).splitlines()[0] if str(e) else type(e).__name__}
        if out.get("error"):
            self._log(run, f"wrote a cover letter for this job, but couldn't save it as a PDF ({out['error'][:160]}), "
                           "so your usual cover letter goes in, if you have one")
            return False
        if out.get("warning"):  # (longer than a page: render_document marked it, and no upload takes it)
            self._log(run, "wrote a cover letter for this job, but it came out longer than a page, so your usual "
                           "cover letter goes in, if you have one")
            return False
        run.letter = Path(out["path"]).name
        self._log(run, f"wrote a cover letter for this job ({letters.BY})")
        return True

    # ------------------------------------------------- where a job meets its letter
    async def _held_for_first_letter(self, run: Run) -> bool:
        """Before the job's tab opens: its first letter, waited for while it's being written (LETTER_FIRST_WAIT), on its
        card for the person's OK (True: paused). A draft from before the OK, kept over a restart, goes into the folder
        once the OK has been given."""
        job = self.srv.tracker().get(run.job_id, with_description=False) or {}
        draft = draft_of(job)
        if draft is None:
            return False
        if has_letter(job):  # (a letter in its folder is the one that goes: a draft beside it holds nothing)
            if draft.is_file():
                self._drop_draft(run.job_id)
            return False
        task = self._letter_tasks.get(run.job_id)
        if not self.letters_approved and task is not None and not task.done():
            run.reason = "Writing its cover letter (the first one, for you to check)…"
            self._log(run, "waiting for its cover letter, the first one, still being written")
            run.letter_wait = True  # (the letter written meanwhile is the one it waits for: _write_letter_now)
            try:
                await asyncio.wait({task}, timeout=LETTER_FIRST_WAIT)
            finally:
                run.letter_wait = False
            if not task.done():
                self._log(run, "its cover letter wasn't written in time: it went on without one")
        if not draft.is_file():
            return False
        text = draft.read_text(encoding="utf-8")
        if self.letters_approved:
            await self._letter_into_folder(run, text)
            self._drop_draft(run.job_id)
            return False
        run.letter_text = text
        self._pause(run, "cover_letter", LETTER_SAYS, code="cover_letter")
        return True

    async def _letter_ready(self, run: Run, data: dict[str, Any]) -> float:
        """A cover letter box on the page while the job's letter is still being written: waited for, up to
        LETTER_WAIT seconds, so the fill uploads it; then on without it (the usual cover letter, if any). The seconds
        waited."""
        task = self._letter_tasks.get(run.job_id)
        # (not for a first letter, before the OK: written now, it never goes into this job's application)
        if task is None or task.done() or not cover_box(data) or not self.letters_approved:
            return 0.0
        self._log(run, "waiting for its cover letter, still being written")
        began = time.monotonic()
        await asyncio.wait({task}, timeout=LETTER_WAIT)
        if not task.done():
            self._log(run, "its cover letter wasn't written in time: your usual one goes in, if you have one")
        return time.monotonic() - began

    async def _stop_letters(self) -> None:
        """The desk stopping: the letters being written are dropped (a job's is written again when it's queued)."""
        tasks = list(self._letter_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        self._letter_tasks.clear()

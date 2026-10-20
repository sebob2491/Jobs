"""Filling a job's pages, for Applier (a mixin): the person's answers for one application, the profile's
(srv.autofill), a saved draft's changes kept, and the entry blocks left short, added past the profile's or unfit
for it."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from . import config
from .autofill import (classes_only, clean_label, degree_kinds_listed, degree_not_listed, entry_of, is_empty_value,
                       no_choice_for_no_degree, norm, site_default, unset_choice)
from .browser import final_text

if TYPE_CHECKING:
    from .pipeline import Run


def _flagged(box: dict[str, Any], page: dict[str, Any]) -> bool:
    """Does the page say this box is wrong: marked invalid, an error of its own, or one of the page's errors naming
    it?"""
    said = clean_label(box.get("label") or "").lower()
    return bool(box.get("invalid") or box.get("error")
                or said and any(said in str(e).lower() for e in page.get("errors") or []))


class FillingMixin:
    """Applier's filling."""

    if TYPE_CHECKING:  # (the applier's own)
        srv: Any

        def _log(self, run: Run, text: str) -> None: ...

        async def _look(self, text: bool = True) -> tuple[dict[str, Any], str]: ...

    async def _fill_once(self, run: Run, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Answers the person gave for this application only (not remembered). Returns the
        ones that didn't go in, by question: the field, the reason, and the entries to
        choose from when the answer was a group of them.

        An answer that didn't go in, or went in and was gone a moment later, gets a second
        try before the person is asked for it again: Eightfold's dropdowns (Lam's and
        Micron's consent questions, live) now and then take one only the second time."""
        if not run.once:
            return {}
        filled: set[str] = set()
        failed: dict[str, dict[str, Any]] = {}
        dropped: set[str] = set()  # (optional ones the site emptied after the second try: left empty)
        # (the page as it was, and each box an answer was sent to, with its key: for the "What went in" panel)
        first = data
        sent: dict[str, str] = {}
        for attempt in range(2):
            fields = {f["id"]: f for f in data.get("fields") or []}
            # a box's own answer (the desk's "section | label | sub-label", for a label more than
            # one box has), else the answer to its label
            by_id = {fid: placed if (placed := pipeline.placed_key(f)) in run.once
                     else pipeline.question_key(f.get("label") or "") for fid, f in fields.items()}
            # an answer stays with the page it first went in on: "If yes, please explain" about
            # relatives isn't the answer to a later page's "If yes, please explain" (the same step with its errors
            # shown is the same page: Robert Half's Workday, run 59, came back with "Errors Found" over it and an
            # answer lost, which was never put back)
            here = pipeline._answers_page(data)
            if attempt == 0:  # (an answer that is the site's first choice a list shows: the person's now, never asked
                # again for being the site's: the review of 0.3.138)
                same = [fid for fid, key in by_id.items() if key in run.once and run.once_page.get(key, here) == here
                        and site_default(fields[fid]) and norm(run.once[key]) == norm(fields[fid].get("value"))]
                if same:
                    await self.srv.browser.mark_judged(same, [])
            fills = [{"id": fid, "value": run.once[key]} for fid, key in by_id.items()
                     if key in run.once and run.once_page.get(key, here) == here
                     # (never an optional box left empty before, while it still is optional and nothing says it's
                     # wrong: once required, or flagged, it's filled again: the third review of run 147's fixes)
                     and not (pipeline.placed_key(fields[fid]) in run.once_left and not fields[fid].get("required")
                              and not _flagged(fields[fid], data))
                     # (or still on its own "Not Specified", which isn't an answer: Kforce's, the probe of Oct 12; or on
                     # the site's first choice where the answer is another: the review of 0.3.138)
                     and (is_empty_value(fields[fid].get("value")) or unset_choice(fields[fid])
                          or site_default(fields[fid]) and norm(run.once[key]) != norm(fields[fid].get("value")))]
            if not fills:
                break
            sent.update({f["id"]: by_id[f["id"]] for f in fills})
            run.did("", [fields[f["id"]] for f in fills if f["id"] in fields], data)
            out = await self.srv.fill_form(fills)
            failed = {by_id[r["id"]]: {**{k: fields[r["id"]][k] for k in ("id", "kind", "label", "section", "required",
                                                                          "options")
                                          if fields[r["id"]].get(k) not in (None, [])},
                                       "error": r.get("error") or "didn't take",
                                       **({"options": r["options"]} if r.get("options") else {})}
                      for r in out.get("results", []) if not r.get("ok") and r.get("id") in fields}
            filled |= {by_id[f["id"]] for f in fills} - set(failed)
            for key in {by_id[f["id"]] for f in fills} - set(failed):
                run.once_page.setdefault(key, here)
            await asyncio.sleep(pipeline.ONCE_SETTLE)  # long enough for an answer that won't stick to be gone
            data, _ = await self._look(text=False)
            if attempt == 1:
                # gone again after the second try: the site won't keep it (UKG Pro's number box, run 147, empties itself
                # of anything but a number, and the desk said it filled the answer and asked for it again each round).
                # A required one goes back to the person, said so; never another answer made up in its place. An
                # optional one is left empty, and said in the log
                now = {f.get("id"): f for f in data.get("fields") or []}
                for f in fills:
                    key, box = by_id[f["id"]], now.get(f["id"])
                    if key in failed or box is None or not (is_empty_value(box.get("value")) or unset_choice(box)):
                        continue
                    asked = fields[f["id"]]
                    number = asked.get("numeric") or asked.get("input_type") == "number"
                    if not asked.get("required") and not box.get("required") and not _flagged(box, data):
                        # (that box alone, by its own key, is not tried again on every look at the page: the answer
                        # stays for any other box its label's key answers, the second review of run 147's fixes)
                        dropped.add(key)
                        run.once_left[pipeline.placed_key(asked)] = pipeline.question_key(asked.get("label") or "")
                        self._log(run, "the site didn't keep your answer to the optional "
                                  f"“{pipeline._short(clean_label(asked.get('label') or '') or 'question')}”"
                                  + (" (the box takes a number)" if number else "") + ", so it's left empty")
                        continue
                    failed[key] = {**{k: asked[k] for k in ("id", "kind", "label", "section", "required", "options")
                                      if asked.get(k) not in (None, [])},
                                   "error": "the site didn't keep your answer" + (
                                       ": this box takes a number" if number else ", and emptied the box again")}
        filled -= set(failed) | dropped
        pipeline._went_in(run, [{"id": fid, "value": run.once.get(key)} for fid, key in sent.items() if key in filled],
                          first, data)
        if filled:
            self._log(run, f"filled {len(filled)} answer(s) you gave for this application")
        if failed:
            self._log(run, f"{len(failed)} of your answers didn't go in")
        return failed

    async def _autofill(self, run: Run, data: dict[str, Any], own: set[str] | None = None,
                        replacing: set[str] | None = None) -> dict[str, Any]:
        """The profile's answers into the page's empty boxes (srv.autofill). On a saved draft's pages (Run.draft: gone
        in through the site's "Continue Application", issue #112, note 38), once a page and only until the job first
        pauses after going in (from then on the person may be at work in its tab: a Resume, a tab the queue carried on
        with by itself, a card answered, and what they changed there is theirs), also into boxes that show another
        answer than the profile's: the draft's, given on an earlier visit, may be out of date. Never a box the profile
        doesn't answer, a self-identification or how-heard answer, this application's own answers (run.once), nor any
        other plan_autofill's refresh leaves alone. Said in the log either way. A box that won't take the profile's
        answer is read again: what it holds is said, and one left empty is asked as any box a fill didn't go into."""
        page = pipeline._answers_page(data)
        checking = run.draft and not run.draft_off and not run.hands_on
        before = {str(f.get("id")): f for f in data.get("fields") or [] if isinstance(f, dict)}

        def replaced(filled: list[dict[str, Any]]) -> list[tuple[dict[str, Any], Any, Any]]:
            # what else went into a box the draft had filled, and changed it (the third review: a preset country set
            # right, a list on its first choice answered again): the draft's answers changed all the same. Never a box
            # this pass filled itself (filled_ids: its "before" is the desk's own fill), nor one whose file kept from
            # before the desk replaced (`replacing`: said by _replaced_in, whether the draft is re-checked or not)
            out = []
            for f in filled:
                box = before.get(str(f.get("id"))) or {}
                old = box.get("value")
                if f.get("refreshed") or str(f.get("id")) in (own or set()) | (replacing or set()) \
                        or is_empty_value(old) or unset_choice(box) or norm(old) == norm(f.get("value")):
                    continue
                out.append((box, old, f.get("value")))
            return out
        if not checking or page in run.draft_pages:
            result = await self.srv.autofill(job_id=run.job_id)
            run.did("", result.get("filled"), data)
            if checking and (others := replaced(result.get("filled") or [])):
                self._draft_changes(run, others, data)
            return result
        run.draft_pages.add(page)
        keep = [str(f["id"]) for f in data.get("fields") or [] if f.get("id") is not None
                and (pipeline.placed_key(f) in run.once or pipeline.question_key(f.get("label") or "") in run.once)]
        result = await self.srv.autofill(job_id=run.job_id, refresh=True, keep=keep)
        run.did("", result.get("filled"), data)
        changed = [f for f in result.get("filled") or [] if f.get("refreshed")]
        refused = [f for f in result.get("failed") or [] if f.get("refreshed")]
        # what each box the refresh went into held, and holds now (for the job's "What went in" panel alone: the log,
        # notes and reports never say a draft's answer, which the profile no longer holds and no scrubber knows)
        changes: list[tuple[dict[str, Any], Any, Any]] = [
            (before.get(str(f.get("id"))) or {"id": f.get("id"), "label": f.get("label")},
             (before.get(str(f.get("id"))) or {}).get("value"), f.get("value")) for f in changed]
        changes += replaced(result.get("filled") or [])
        kept: list[dict[str, Any]] = []
        emptied: set[str] = set()
        if refused:
            now = {str(f.get("id")): f for f in (await self._look(text=False))[0].get("fields") or []}
            emptied = {str(f.get("id")) for f in refused if is_empty_value(now.get(str(f.get("id")), {}).get("value"))}
            # (one left empty is a fill that didn't go in, as any other: asked; one holding
            # something, said by its label)
            result["failed"] = [f for f in result.get("failed") or [] if not f.get("refreshed")
                                or str(f.get("id")) in emptied]
            kept = [f for f in refused if str(f.get("id")) not in emptied and str(f.get("id")) in now]
            for f in refused:  # (half typed, or emptied: changed all the same)
                box, held = before.get(str(f.get("id"))) or {}, now.get(str(f.get("id")), {}).get("value")
                if (str(held if held is not None else "")
                        != str(box.get("value") if box.get("value") is not None else "")):
                    changes.append((box or {"id": f.get("id"), "label": f.get("label")}, box.get("value"), held))
        if changes:
            self._draft_changes(run, changes, data)

        def named(fields: list[dict[str, Any]]) -> str:
            return ", ".join(dict.fromkeys(f"“{pipeline._short(clean_label(f.get('label') or '') or 'a field')}”"
                                           for f in fields))
        where = pipeline._where(data)
        if changed:
            self._log(run, f"re-checked the saved draft's answers on {where}: {len(changed)} field(s) showed another "
                      f"answer than your profile's, so I put your profile's in: {named(changed)}")
        elif not refused and (not replacing or any(not is_empty_value(f.get("value")) for fid, f in before.items()
                                                   if fid not in replacing)):
            # (never of a box whose kept file the desk replaces: that's said on its own. With no other box showing an
            # answer, there's nothing to say here)
            self._log(run, f"re-checked the saved draft's answers on {where}: they match your profile")
        if refused:
            self._log(run, f"re-checked the saved draft's answers on {where}: {len(refused)} field(s) wouldn't take "
                      "your profile's answer" + (f"; as they are now: {named(kept)}" if kept else "")
                      + (f"; {len(emptied)} left empty, so they're asked" if emptied else ""))
        return result

    def _draft_changes(self, run: Run, changes: list[tuple[dict[str, Any], Any, Any]], page: dict[str, Any]) -> None:
        """A saved draft's answers the desk changed: never sent by Submit for me (the person checks them first:
        _finish), each on the job's "What went in" panel, and both kept in the job's own folder for after a restart of
        the desk (_save_draft_changes)."""
        run.draft_changed = True
        pipeline._went_changed(run, changes, page)
        pipeline._save_draft_changes(self.srv, run)

    def _forget_draft_changes(self, run: Run) -> None:
        """The job went in (the person's Submit, or marked applied): its draft's changes are no longer kept."""
        pipeline._forget_draft_changes(self.srv, run.job_id)

    def _draft_off(self, run: Run) -> None:
        """A draft's answers are re-checked no more for this job (Run.draft_off), said once in its log, and kept in its
        folder: after a restart of the desk, a draft the person had taken over isn't re-checked either."""
        if run.draft and not run.draft_off:
            run.draft_off = True
            self._log(run, "draft not re-checked from here on: what you change in the desk's "
                           "browser window stays as you leave it")
            pipeline._keep_draft_off(self.srv, run.job_id)

    def _note_skipped(self, run: Run, result: dict[str, Any]) -> None:
        """Optional fields that wouldn't take the profile's answer are skipped, and said so. So
        are optional ones whose choices don't have it (Qorvo's veteran list has no "I don't
        wish to answer"): left empty, since no other choice is the person's. Each said once."""
        said = []
        skipped = [f.get("label") or "a field" for f in result["failed"] if f.get("required") is False]
        if skipped:
            said.append(f"skipped {len(skipped)} optional field(s) that wouldn't take your profile's answer: "
                        + ", ".join(f"\u201c{pipeline._short(label)}\u201d" for label in skipped[:3]))
        unoffered = [f for f in result.get("needs_input") or [] if f.get("unmatched") and not f.get("required")]
        if unoffered:
            said.append(f"left {len(unoffered)} optional question(s) empty, as none of their choices is your profile's "
                        "answer: " + ", ".join(f"\u201c{pipeline._short(f.get('label') or 'a field')}\u201d (yours: "
                                               f"\u201c{f['unmatched']}\u201d)" for f in unoffered[:3]))
        for line in said:
            if line not in run.log:
                self._log(run, line)

    async def _drop_unfit_school(self, run: Run, data: dict[str, Any], pending: list[dict[str, Any]]) -> bool:
        """A school attended without finishing a degree, whose block's Degree list has nothing for that (issue #112,
        note 26, the owner's call): the desk deletes that block with its own Delete and carries on (the resume
        still shows the classes; a degree not earned is never picked). Only the page's last school block, and
        never its only one (the site may need one): a block before others would hand its number to the next
        school's. Once a block; any other way, the person's (_entry_gaps says so). True when it deleted one."""
        prof = config.Profile.load()
        # (only where the profile says no degree was finished there: not a degree it lacks, nor one under way)
        unfit = {str(q.get("section") or "") for q in pending
                 if no_choice_for_no_degree(q, prof) and classes_only(q, prof)} - {""}
        if not unfit:
            return False
        school_block = re.compile(r"education\b.*\d\s*$", re.I)
        deletes = [a for a in data.get("actions") or [] if not a.get("disabled")
                   and re.match(r"^(?:delete|remove)\b", final_text(a.get("text") or ""), re.I)]
        # the blocks by their boxes, and by their Deletes (a block's boxes can be drawn a moment after it)
        blocks = sorted({str(x.get("section") or "") for x in [*(data.get("fields") or []), *deletes]
                         if school_block.match(str(x.get("section") or ""))},
                        key=lambda s: int(re.findall(r"\d+", s)[-1]))
        last = blocks[-1] if blocks else ""
        if len(blocks) < 2 or last not in unfit:
            return False
        delete = pipeline._own_delete(deletes, last)
        if delete is None or last in run.schools_tried:
            return False
        run.schools_tried.append(last)  # (pressed once a block, whatever comes of it)
        run.did("pressed", delete)
        try:
            await self.srv.click(delete["id"])
        except Exception:  # gone, or it won't take a click: the person's, as before
            return False
        await asyncio.sleep(0.5)  # (the block goes from the page)
        now, _ = await self._look(text=False)
        if any(f.get("section") == last for f in now.get("fields") or []):
            return False  # still there (a "Are you sure?" over it, say): the person's
        run.schools_dropped.append(last)  # gone: one block fewer of the profile's schools from here on
        pipeline._went_forget(run, data, last)  # (and what went into it, off the "What went in" panel)
        found = next((entry_of(q, prof) for q in pending if q.get("section") == last and entry_of(q, prof)), None)
        school = found[0] if found else last
        self._log(run, f"deleted {last} ({school}): its Degree list has no choice for classes without a degree, and "
                  "the desk never picks a degree you didn't earn (your resume still shows the classes)")
        return True

    async def _drop_added_extra(self, run: Run, data: dict[str, Any], pending: list[dict[str, Any]],
                                added: dict[str, int], tried: set[str], filled: set[str] | None = None,
                                given: set[str] | None = None) -> bool:
        """A block past the profile's entries, with nothing in it, where the desk added a block of that kind on this
        pass: Workday's My Experience drew the account's saved school (or job) after the desk had looked for it and
        added one (Hitachi High-Tech America's, run 92; U.S. Bank's and Baker Tilly's, runs 52 and 62), and the page
        then had more blocks than the profile has entries. The empty one goes with its own Delete, as many as the desk
        added (`added`, by kind: taken down as they go; or, on a later pass, the block the desk's mark is on: KLA's,
        run 147), only the last block of its kind, never its only one (a block before others would hand its number on;
        a marked one numbered 2 or more isn't, though the ones before it aren't drawn yet), and only the very block an
        Add the desk pressed drew (browser_blocks.py's added_blocks); once a block (`tried`). A block with anything in
        it, or one the desk didn't add, stays the person's (_entry_gaps says so). Where the site drew the saved entry
        INTO the desk's block (Workday numbers its blocks by place: Arizona State University's, run 108, the desk's mark
        left on a block now holding the saved job, and a new empty block after it, unmarked), the marked block holds a
        value the desk never put there (a text box or list neither among `filled`, the boxes its own fills went into on
        this pass, by id, nor showing one of `given`, what they put there: a box drawn again under a new id, or a
        once-answer, is still the desk's), and the empty last block is the desk's own. True when it deleted one."""
        prof = config.Profile.load()
        extra: dict[str, str] = {}  # block: its kind
        for q in pending:
            found = entry_of(q, prof)
            if found is not None and found[1] == "extra" and q.get("section"):
                extra[str(q["section"])] = "education" if "no school" in found[0] else "work"
        deletes = [a for a in data.get("actions") or [] if not a.get("disabled")
                   and re.match(r"^(?:delete|remove)\b", final_text(a.get("text") or ""), re.I)]
        for block, kind in extra.items():
            if block in tried:
                continue
            numbered = re.compile(rf"^(?:{pipeline._BLOCK_KINDS[kind]})\s+(\d+)\s*$", re.I)
            # the blocks of its kind by their boxes, and by their Deletes (a block's boxes can be drawn after it)
            blocks = sorted({str(x.get("section") or "") for x in [*(data.get("fields") or []), *deletes]
                             if numbered.match(str(x.get("section") or ""))},
                            key=lambda s: int(re.findall(r"\d+", s)[-1]))
            if not blocks or blocks[-1] != block:
                continue
            # the very block the desk's own press drew (browser_blocks.py's add_entries marks it), never a saved one
            # drawn beside it, empty for a moment (the review of 0.3.173): a block it can't tell so is the person's.
            # The mark says so on a later pass too: the person's Carry on, where this pass added none (`added` is the
            # pass's own; KLA's Workday, run 147, stopped again on the desk's own empty block)
            marked = [h for h in await self.srv.browser.added_blocks() if numbered.match(h)]
            own = pipeline._block_key(block) in {pipeline._block_key(h) for h in marked}
            if added.get(kind, 0) < 1 and not own:
                continue
            # never its kind's only block: one the desk's own Add drew, numbered 2 or more, isn't, though the blocks
            # before it aren't read yet (KLA's, run 147: Workday's "refresh" reload drew the saved job above the desk's
            # block late, its boxes and Delete not there yet)
            if len(blocks) < 2 and not (own and int(re.findall(r"\d+", block)[-1]) >= 2):
                continue
            boxes = [f for f in data.get("fields") or [] if f.get("section") == block]
            if not boxes or any(not is_empty_value(f.get("value")) and not unset_choice(f) for f in boxes):
                continue  # (something in it: someone's, never deleted)
            delete = pipeline._own_delete(deletes, block)
            if delete is None:
                continue
            # Unless the mark sits on a block of this kind the site filled (values in it the desk never typed: the saved
            # entry drawn into the desk's own, the empty one after it then the desk's)
            reused = not own
            if reused:
                def site_filled(heading: str) -> bool:
                    # a text box or list with a value the desk didn't put there: not one it filled (by id), nor showing
                    # what it or the person gave (a box drawn again under a new id, a once-answer: the review's), nor a
                    # list left on the site's own default; never a date's part or a tick
                    return any(pipeline._block_key(str(f.get("section") or "")) == pipeline._block_key(heading)
                               and f.get("kind") in pipeline._TEXTLIKE_KINDS and f.get("role") != "spinbutton"
                               and str(f.get("id")) not in (filled or set())
                               and norm(f.get("value")) not in (given or set())
                               and not f.get("judged") and not site_default(f)
                               and not is_empty_value(f.get("value")) and not unset_choice(f)
                               for f in data.get("fields") or [])
                if not any(site_filled(h) for h in marked):
                    continue
            tried.add(block)  # (pressed once a block, whatever comes of it)
            run.did("pressed", delete)
            try:
                await self.srv.click(delete["id"])
            except Exception:  # gone, or it won't take a click: the person's, as before
                return False
            await asyncio.sleep(0.5)  # (the block goes from the page)
            now, _ = await self._look(text=False)
            if any(f.get("section") == block for f in now.get("fields") or []):
                return False  # still there (a "Are you sure?" over it, say): the person's
            added[kind] = max(0, added.get(kind, 0) - 1)
            thing = "schools" if kind == "education" else "jobs"
            self._log(run, f"deleted {block}: an empty block past your profile's {thing}, " + (
                f"left over after the page drew the {thing} saved there into the one I had added" if reused
                else f"one I had added while the page was still drawing the {thing} saved there"))
            return True
        return False

    def _note_short_entries(self, run: Run, section: str, added: dict[str, Any], data: dict[str, Any]) -> None:
        """A section whose Add button stayed covered, with fewer blocks on the page than the profile has entries
        (Workday's My Experience, issue #112, note 32: 2 of 5 jobs reached the form, and on another site the job went
        on to its review page with its school missing), is remembered on the run: the job stops for it. Its blocks
        are counted by their own Deletes too: UKG Pro's, filled from the resume, have no numbered heading ("Delete
        Work Experience 1"; run 44 stopped Nikon, UCT and Desert Financial on "0 of your 1 job")."""
        after, wanted = max(added.get("after", 0), pipeline._blocks_by_delete(data, section)), added.get("wanted", 0)
        if not added.get("add_button_covered") or after >= wanted:
            run.entries_short.pop(section, None)
            return
        run.entries_short[section] = (after, wanted)
        if run.entries_ok.get(section) == (after, wanted):
            self._log(run, f"went on with {pipeline._blocks(section, after, wanted)}: the Add button is still covered, "
                      "and you pressed Carry on for it")

    def _short_entries(self, run: Run) -> dict[str, tuple[int, int]]:
        """The sections left short that the person hasn't pressed Resume on, as they stand (one pressed on, with the
        shortfall unchanged, is gone on with: pressing Resume on a page nothing was added to would only stop again)."""
        return {s: n for s, n in run.entries_short.items() if run.entries_ok.get(s) != n}

    def _entry_gaps(self, run: Run, data: dict[str, Any],
                    pending: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], str]:
        """Boxes in a job's or school's block ("Work Experience 2": its From and To) whose answer
        lives in the profile's work_history or education_history are never asked one by one:
        twenty bare "From" and "To" boxes on the desk couldn't say whose they were, and an answer
        typed there would go in every one. What's said instead (which entries lack what, or
        which of the profile's answers the site didn't take), and the questions left to ask.
        A school is named with its block on the page ("Education 2"): one school can have two. And the stop's code
        (STOP_CODES), by what it's mostly about: a Degree list, a box that refused the profile's answer, a list that
        didn't load, or entries the profile lacks."""
        prof = config.Profile.load()
        missing: dict[str, dict[str, list[str]]] = {"work": {}, "education": {}, "extra": {}}
        refused: dict[str, list[str]] = {}  # the profile has it; the page didn't take it
        no_choice: list[str] = []  # Degree lists with nothing for classes without a degree
        unlisted: dict[str, str] = {}  # Degree lists with nothing for the profile's degree (a GED's)
        # Degree lists with several kinds of it (BA, BS) and the profile naming none
        kinds: dict[str, tuple[str, list[str]]] = {}
        unloaded: dict[str, list[str]] = {}  # a school's lists with their placeholder alone: not loaded
        empty_lists = pipeline._unloaded_lists(pending, data, prof)
        rest = []
        for q in pending:
            found = entry_of(q, prof)
            if found is None:
                rest.append(q)
                continue
            name, kind = found
            block = str(q.get("section") or "")
            if kind == "education" and block and block != name:
                name = f"{name} in {block}"
            if any(q is e for e in empty_lists):
                what = clean_label(q.get("label") or "").strip() or "a list"
                if what not in unloaded.setdefault(name, []):
                    unloaded[name].append(what)
                continue
            if no_choice_for_no_degree(q, prof):
                if name not in no_choice:
                    no_choice.append(name)
                continue
            if (degree := degree_not_listed(q, prof)) is not None:
                if listed_kinds := degree_kinds_listed(q, degree):
                    kinds.setdefault(name, (degree, listed_kinds))
                else:
                    unlisted.setdefault(name, degree)
                continue
            what = clean_label(q.get("label") or "").strip() or "a box"
            into = refused if q.get("error") else missing[kind]
            if what not in into.setdefault(name, []):
                into[name].append(what)

        def listed(entries: dict[str, list[str]]) -> str:
            return "; ".join(f"{name} ({', '.join(what)})" for name, what in entries.items())

        site, said = pipeline._site(run, data), []
        if missing["work"]:
            said.append(f"{site} asks about your past jobs, and your profile doesn't have this for them: "
                        f"{listed(missing['work'])}. Add it to those jobs under work_history in profile.yaml (dates "
                        "as start: 2021-03 and end: 2023-06, or end: present), or ask Claude: \u201cadd the start and "
                        "end months of my jobs from my resume to my profile\u201d.")
        if missing["education"]:
            # what each missing box takes: a Degree box isn't dates, and coursework is never a degree
            asked = " ".join(w for boxes in missing["education"].values() for w in boxes).lower()
            how = []
            if re.search(r"degree|qualification|diploma", asked):
                how.append("degree: the one you earned there, or Some college (no degree) where you took classes "
                           "without finishing one")
            if re.search(r"major|field|study|discipline|concentration", asked):
                how.append("major: your field of study")
            if re.search(r"from|to\b|start|end|date|year|month|graduat", asked) or not how:
                how.append("years as start: 2016 and end: 2018")
            said.append(f"{site} asks about your schools, and your profile doesn't have this for them: "
                        f"{listed(missing['education'])}. Add it to those schools under education_history in "
                        f"profile.yaml ({'; '.join(how)}), or ask Claude to add it from your resume (only what it "
                        "says: never a degree you didn't finish).")
        if said:
            said.append("Then press Carry on: the desk fills them in from your profile on every application after "
                        "that. Or type them into the page in the desk's browser window and press Carry on.")
        if missing["extra"]:
            said.append(f"The page has more blocks than your profile has entries: {listed(missing['extra'])}. Remove "
                        "the extra block in the desk's browser window (or fill it in there), then press Carry on.")
        if refused:
            said.append(f"{site} didn't take your profile's answer for: {listed(refused)}. Fill those in the desk's "
                        "browser window (in a list, the closest choice), then press Carry on.")
        if no_choice:
            # "Some college (no degree)" in the profile can't go there, and coursework is never a degree
            said.append(f"The Degree list for {'; '.join(no_choice)} has no choice for classes without a degree, so "
                        "that box is yours (the desk never picks a degree you didn't earn): choose in the desk's "
                        "browser window, or delete that school's block with its Delete button where the site doesn't "
                        "need it (your resume still shows the classes), then press Carry on. If you did finish a "
                        "degree there, add it to that school under education_history in profile.yaml (degree: the "
                        "one you earned), and the desk fills it in from then on.")
        if kinds:
            # the list has the level, in kinds the profile doesn't choose between: never one it didn't say
            said.append("; ".join(f"The Degree list for {name} has several kinds of {degree} ({', '.join(options[:4])})"
                                  for name, (degree, options) in kinds.items())
                        + ", and your profile doesn't say which you earned, so that box is yours: choose it in the "
                        "desk's browser window, then press Carry on. Or write it in full under education_history in "
                        "profile.yaml (degree: Bachelor of Science, say), and the desk picks it from then on.")
        if unloaded:
            one = sum(len(w) for w in unloaded.values()) == 1
            said.append(f"{site}'s {'list' if one else 'lists'} for {listed(unloaded)} showed no choices at all, only "
                        f"{'its' if one else 'their'} placeholder: the page didn't finish "
                        f"loading {'it' if one else 'them'}"
                        + (", even after I reloaded it" if run.list_reloaded else "")
                        + ". Reload the page in the desk's browser window (or try again later), then press Carry on.")
        if unlisted:
            # the profile has it; the list doesn't (no high school or GED choice: issue #112, note 16)
            said.append("The Degree list has no choice for " + "; ".join(f"your {degree} ({name})" for name, degree in
                                                                         unlisted.items())
                        + ", so that box is yours: choose the closest honest one in the desk's browser window "
                          "(“Others” or the like, where it's there; never a degree you "
                          "didn't earn), then press Carry on.")
        return " ".join(said), rest, ("degree_list" if no_choice or kinds or unlisted else "box_refused_answer"
                                      if refused else "page_not_loaded" if unloaded else "profile_gap")


# pipeline.py's own names (its waits, patterns, helpers and classes) are read from it as pipeline.<name> when a method
# runs: a test's patch of one (monkeypatch.setattr(pipeline, ...)) reaches the code here as it does pipeline.py's.
# Imported last: pipeline.py imports this module for Applier's bases, and either may be imported first.
from . import pipeline  # noqa: E402

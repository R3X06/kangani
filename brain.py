"""Claude API integration -- the "brain" that decides whether to chat or call
a tool, using Anthropic's native tool use / manual agentic loop.
"""

import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

import database
import tools

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
# 2048 was too tight: a bulk request ("10 min before every lesson for 3 weeks")
# emits dozens of create_reminder blocks in one round, and the response was
# being cut off mid-JSON -- stop_reason came back "max_tokens", the loop fell
# out with no text block, and the user got the generic "couldn't come up with
# a response" while zero reminders had actually been created.
MAX_TOKENS = 8192
MAX_TOOL_ITERATIONS = 10  # combined calendars can chain topic-lookup +
# query_schedule + query_tasks + query_notes + query_reminders in one turn
# A truncated turn is retried with a smaller-batches nudge rather than
# surfaced as a failure -- but only so many times, so a genuinely impossible
# request can't spin.
MAX_TRUNCATION_RETRIES = 2
# Counted in raw message entries, not turns -- one user turn can now expand
# into several (assistant tool_use -> user tool_result -> ... -> assistant
# text), all of which are kept so a follow-up knows what already happened.
HISTORY_LIMIT = 40

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    # Built lazily instead of at import time, so it's not created before
    # bot.py's load_dotenv() has populated ANTHROPIC_API_KEY into os.environ.
    # Public so other modules (e.g. pdf_import) can reuse the single client
    # rather than each building their own.
    global _client
    if _client is None:
        _client = AsyncAnthropic()
    return _client


def _get_client() -> AsyncAnthropic:
    # Backwards-compatible alias for internal callers.
    return get_client()


_CACHE = {"type": "ephemeral"}

# The system prompt is split in two so the large guidance block is byte-for-byte
# identical on every request and can sit behind a cache breakpoint. A single
# interpolated character anywhere inside it -- the clock, say -- would change
# the prefix and invalidate the cache on every call, which is why the date/time
# anchors and the offset example derived from them live in a separate trailing
# block instead of near the top where they used to be.
_STATIC_SYSTEM = """You are Kangani, a personal assistant running inside Telegram. \
You help the user manage a unified tree of topics (courses, modules, events, \
life areas) and the tasks, notes, reminders, and timetable lessons attached \
to them.

Use the available tools whenever the user's request involves creating, \
updating, or querying tasks, modules, topics, notes, schedule blocks, \
events, or reminders stored in the database. For general conversation, \
clarification, or anything not backed by a tool, respond directly in \
plain text.

Everything the user tracks lives in ONE tree of topics: courses, modules, \
events, and freeform life areas are all topics, nestable to any depth. A \
task, note, or reminder can attach to any topic via topic_id, or to nothing \
at all. Before creating a topic, call list_topics to avoid near-duplicates, \
and call list_topic_kinds to reuse an existing `kind` label rather than \
minting a near-synonym. `kind` is a free string (canonical ones: course, \
year, semester, module, component, event); matching is case-insensitive. Use \
kind='module' for an academic subject that appears on the timetable -- it \
gets a stable auto-assigned color. If a task or note doesn't obviously belong \
under a topic, you may leave it unattached: offer a topic once, but don't \
force one and don't re-ask if the user declines. If a request is genuinely \
ambiguous, ask a brief clarifying question rather than guessing.

Topics can be nested into subtopics to any depth. Topic names are NOT \
guaranteed unique -- always call list_topics first to find the right \
topic_id before referencing an existing topic in add_note, query_notes, \
create_task, create_reminder, or as a parent_topic_id in create_topic. \
Never guess a topic_id.

When the user shares something worth saving and no existing topic clearly \
matches, create a new topic for it (nested under the closest existing \
relevant topic if one exists, otherwise as a new top-level topic under the \
right module) rather than forcing it into an unrelated topic. Do NOT invent \
a catch-all topic the way "General" is used as a fallback module for \
uncategorized tasks -- notes dumped into a generic bucket become unfindable \
by topic later. If it's genuinely unclear what topic or module a note \
belongs to, ask a brief clarifying question. Mark is_reference=true for \
material worth keeping for later lookup (a link, an excerpt, a definition); \
default to false for transient notes or observations.

## Answering "show me..." / calendar / listing requests

When the user asks to see things ("my Y3S1 calendar", "sc2001 labs", "what's \
due", "general reminders"), resolve the request COMPOSITIONALLY. Read the \
request as a set of constraint words plus the noun ("calendar", "schedule", \
"list") which itself carries no constraint. Classify EACH constraint word \
into exactly one of four independent axes, then return the INTERSECTION of \
all of them:

1. SCOPE -- a topic name (a year, semester, module, event, or any topic). \
Resolves via list_topics to that topic AND everything nested beneath it (its \
subtree). Pass it as topic_id with include_subtopics=true (the default). If \
NO topic is named, scope is everything the user owns -- no topic filter.

SCOPE SHORTHAND: if a scope word doesn't match any single topic name exactly, \
but decomposes into a sequence of topics that ARE nested inside each other \
(e.g. "Y3S1" doesn't exist as a topic, but "Y3" and "S1" do, and "S1" is a \
child of "Y3"), resolve it as that nested path -- scope is the innermost one \
("S1" under "Y3"), same as if the user had said "Y3 S1" or "S1 under Y3". \
Match each piece against BOTH a topic's name AND its nickname: if the user \
nicknamed "Year 1" as "Y1" and "Semester 1" as "S1", then "Y1S1" resolves to \
S1-under-Y1 by matching the nicknames, exactly as it would by name. \
list_topics shows each topic's nickname and full name in parentheses, so use \
those when a raw scope word doesn't match a name directly. This applies to \
EVERY data request -- calendars, schedules, tasks, notes, files, deletes -- \
not just calendars. Try this name-or-nickname decomposition BEFORE concluding \
a scope word is unrecognized. Only fall through to "I don't recognize that \
word" if no such nested-path decomposition exists by name or nickname either.

2. CONTENT TYPE -- which of lessons / tasks / notes / reminders to return. \
If the user names a type ("lessons", "classes", "tasks", "deadlines", \
"notes", "reminders"), return ONLY that type. If they name NO type (e.g. \
"Y3S1 calendar" alone), return the FULL combined view: lessons + tasks + \
reminders + notes, by calling the relevant query tools and presenting them \
together in one reply. The bare word "calendar"/"schedule"/"list" is NOT a \
type word -- it means "combine everything", not "lessons only".

3. FILTER -- a narrowing word WITHIN a content type. A lesson type \
(lecture, tutorial, lab, seminar, or any user-defined one -- resolve via \
list_lesson_types, pass as lesson_types to query_schedule); a task category \
(resolve via list_task_categories, pass as category to query_tasks); or the \
reminder scope word "general" (pass scope='general' to query_reminders). \
Lesson types are SUBTYPES of lessons: "lessons" returns all of them; "labs" \
returns only labs; "lessons" is inclusive of labs.

4. TIME / ORDER -- a date range ("today", "this week", "next Friday", "in \
August") maps to date_from/date_to; "next"/"soonest" means take the earliest \
one; "upcoming" means future-only. Time is its own axis and ANDs in alongside \
scope, type, and filter.

Worked examples (SCOPE then FILTER, "calendar" ignored as a bare noun):
- "Y3S1 calendar" -> scope=Y3S1 subtree, no type named -> full combined view \
(lessons + tasks + reminders + notes under Y3S1).
- "Y3S1 lesson calendar" -> scope=Y3S1, type=lessons -> query_schedule with \
topic_id=Y3S1.
- "sc2001 lab" -> scope=SC2001, filter=lesson-type lab -> query_schedule with \
topic_id=SC2001, lesson_types=['lab'].
- "labs" -> no scope, filter=lab -> query_schedule with lesson_types=['lab'], \
no topic filter (all labs, every topic).
- "Y3S1 labs" -> scope=Y3S1, filter=lab -> all labs under Y3S1.
- "general reminders" -> type=reminders, filter=general -> query_reminders \
scope='general'.
- "all reminders" / "my reminders" -> type=reminders -> query_reminders \
scope='all'.

Every constraint word is a bare filter applied across the widest scope unless \
a scope word narrows WHERE to look: "labs" means all labs everywhere; "Y3S1 \
labs" means labs under Y3S1 only. Absence of a scope word is the widest \
scope, never an error.

Resolving words to axes -- and the rules that keep it honest:
- Resolve filter words against the LIVE vocabularies (list_lesson_types, \
list_task_categories, list_topic_kinds) plus the fixed type words, not \
against a fixed list you assume. Call them when unsure what a word is.
- A word that belongs to exactly ONE axis resolves silently. A word that is a \
genuine member of TWO axes at once (e.g. the user made a task category "lab" \
AND a lesson type "lab", so "sc2001 lab" could mean either) -- ONLY THEN ask \
which they meant. Don't invent collisions; most words resolve cleanly.
- If a word matches NO topic and NO known filter/type ("y3s1 quantum \
calendar" where "quantum" is nothing) -- do NOT silently drop it and return \
the broad result as if it were never said. Say you don't recognize that word \
under that scope, and ask. A silently-dropped filter is the worst outcome: \
the user gets a broad answer that looks complete but ignored their constraint.
- Distinguish "valid filter, zero matches" from "unrecognized word". \
"sc2001 seminar" when SC2001 has no seminars -> "SC2001 has no seminars" \
(the filter resolved, nothing matched). That is DIFFERENT from not knowing \
what a word means. Only claim "you have no X" when X actually resolved to a \
known type/filter.
- "deadlines" means tasks that HAVE a deadline, ordered by date -- a deadline \
is a field on a task, not a separate item type. Use query_tasks and present \
the dated ones in date order.

Past-vs-future default when the user gives NO time words: recurring lessons \
ALWAYS show (they are the timeless weekly timetable). Dated items -- tasks \
with deadlines, reminders, one-off events -- default to NOT-yet-past (from \
today onward), since nobody asking for a calendar wants last month's finished \
items cluttering it. Only include past dated items if the user explicitly \
asks ("show completed", "last week").

Schedule blocks hold a recurring weekly timetable (day_of_week) or one-off \
calendar items (specific_date) -- exactly one of the two is ever set for a \
given block. Standing commitments phrased like "every Tuesday", "each \
Monday", or a recurring class/lecture should use day_of_week; a single \
occurrence phrased like "this Friday" or "on August 15th" should use \
specific_date. Unlike tasks, schedule blocks do NOT need a module at all -- \
omit module_name entirely for non-study blocks (gym, errands, \
appointments); do not invent or fall back to a "General" module for these. \
specific_date, start_time, and end_time for schedule blocks are plain local \
calendar/clock values, not absolute instants -- do NOT attach a UTC offset \
or convert timezones for them the way you do for deadline/trigger_datetime. \
When the user asks "what's on this week", treat "this week" as the \
Monday-Sunday calendar week containing the current local date given at the end of this prompt, unless \
they explicitly ask for something else (e.g. "the next 7 days"). Before \
calling delete_schedule_block, call query_schedule first to find the \
correct schedule_block_id -- never guess it.

Some classes don't run every week -- they alternate (odd/even weeks) or run \
on specific weeks. To handle this, each chat has ONE semester anchor: the \
calendar date week 1 begins (its Monday). From that single date Kangani \
derives which semester week any date falls in. Set or update it with \
set_semester_start when the user tells you the start date (e.g. "week 1 \
starts August 13th"). When creating a recurring schedule block that does NOT \
run every week, set create_schedule_block's week_pattern ('odd', 'even', or \
an explicit list like '2,4,6,8,10,12'); leave it as the default 'every' \
otherwise. A non-'every' pattern cannot be resolved without the anchor, so \
if the user asks for an alternating/specific-week class and no semester start \
date is set yet, ask them for it ONCE (which date is week 1?) before creating \
the block -- don't ask repeatedly once it's set.

Some semesters have a recess/reading/break week partway through with no \
classes. When the user mentions one, call set_recess_weeks with any single \
date that falls within that week (its Monday is fine) -- one date per recess \
week. Kangani's week numbers are the school's OFFICIAL numbers: once recess \
weeks are marked, they are skipped automatically, so "week 8" always means \
the school's week 8 even after a break has shifted the calendar. NEVER track \
or apply a week-number offset yourself in conversation to account for a \
recess -- that is exactly what set_recess_weeks does deterministically, and \
manual arithmetic would drift. set_recess_weeks marks a whole week off, not \
individual classes (use week_pattern to skip specific classes).

A time-boxed one-off activity (a hackathon, a talk, a workshop) is just a \
topic with kind='event' (or 'event:<type>', e.g. 'event:hackathon') and an \
event_datetime. Setting event_datetime on create_topic auto-creates reminders \
before it (default 60 and 30 minutes before; pass reminder_offsets_minutes to \
change them, or call add_event_reminder later to add another lead time). \
Create events with create_topic like any other topic, and attach tasks/notes \
to them by topic_id -- look the id up via list_topics, never guess it.

A topic has THREE independent name fields, never overwriting each other: \
`name` (short display/code, e.g. "SC2001" -- used for matching and \
breadcrumbs), `full_name` (the official long title, e.g. "Data Structures and \
Algorithms" -- set it whenever you learn one, such as from a schedule PDF's \
course title), and `nickname` (a short label the USER chose, e.g. "DSA" -- \
only set this when the user explicitly gives one, never invent one). Use \
set_topic_names to edit any of these after creation; it overwrites the given \
field(s), unlike create_topic's fill-only behavior on an existing match.

If a topic was created in the wrong place in the tree (e.g. a module a PDF \
import landed at the root, or the user just wants to reorganize), use \
move_topic to reparent it -- look up both the topic and its new parent via \
list_topics first, never guess the ids.

How a module's name is DISPLAYED in a timetable listing or image is a \
separate question from which name field is stored -- controlled by \
label_format: 'code' (the default), 'full_name', 'nickname', 'code_nickname' \
("SC2001: DSA"), or 'code_full_name'. query_schedule takes an optional \
label_format for a one-off request ("show full names just this once"). But \
/dayimage, /weekimage, /monthimage, /today, and /week are direct slash \
commands that never reach you, so they always use a SAVED per-chat default -- \
call set_timetable_label_format when the user asks to change how things are \
labeled going forward (e.g. "use my nicknames on the timetable"), not just \
query_schedule's one-off override.

Categories (on tasks) and lesson types (on schedule blocks) are user-defined \
labels that work like topic `kind`: before assigning a NEW one, call \
list_task_categories / list_lesson_types and REUSE an existing label if one \
fits (matching is case-insensitive) rather than minting a near-synonym \
('assignment' vs 'Assignments', 'Tut' vs 'Tutorial'). When you are about to \
create a genuinely new category or lesson type, CONFIRM the name with the \
user first, showing the ones that already exist so they can pick an existing \
one instead -- e.g. "New task category 'assignment' (existing: coursework, \
reading). Create it, or use an existing one?". BUT never block the underlying \
action on that confirmation: create the task (or schedule block) immediately, \
leaving it uncategorized if the user hasn't confirmed the label yet, then \
offer to file it. An uncategorized task that got saved is always better than \
a lost one.

A note attaches to a topic by topic_id, or to nothing at all (a "general" \
note). If a note doesn't clearly belong under any topic, save it as a general \
note (omit topic_id) rather than forcing it somewhere -- offer a topic once if \
one seems fitting, don't force it.

Every task, note, and reminder has a hidden reference tag -- a short random \
code, stable for that item's whole life. Tags are HIDDEN by default: never \
show them in normal listings. Only pass show_tags=true when the user \
explicitly asks to see tags (e.g. "notes -tag", "show tags", "with tags"). \
The tag is how the user can refer to one specific item unambiguously later.

Reminders for lessons are a BULK operation: use add_lesson_reminders, never a loop of create_reminder. A request like "10 minutes before every lesson in Y3S1 for the next 3 weeks" is dozens or hundreds of reminders, and issuing them one at a time runs out of room part-way through -- leaving the user with some of them and a message claiming all of them were set. add_lesson_reminders expands the timetable itself, honours each lesson's week pattern and the chat's recess weeks, skips occurrences already in the past, and skips exact duplicates, so it is safe to retry. For any range longer than about a week, call it with dry_run=true first, tell the user the resulting count, and wait for them to confirm before the real call. create_reminder remains the right tool for a genuine one-off.

Never tell the user something was created, updated, or deleted unless a tool result in this turn actually says so. If a tool failed, returned fewer items than expected, or you ran out of tool calls before finishing, say exactly that and say what did land -- report the number the tool reported, not the number the user asked for. A confident but wrong confirmation is far worse than admitting a partial result, because the user only finds out when the reminder never arrives.

When something is rescheduled or called off, deal with the reminders that were tied to the old time -- reschedule_reminder to move one, cancel_reminder to drop it. Leaving them in place means the user gets pinged for something that isn't happening. Deleting is for things that shouldn't exist at all; a finished task is update_task_status with status='done', not delete_task. delete_topic takes a whole subtree with it, so run it once without confirm, read the counts back to the user, and wait for a clear yes.

Reply in plain text only -- no Markdown formatting."""


def _dynamic_system(tz_name: str, local_str: str, utc_str: str, now_local) -> str:
    """The per-request tail: never cached, deliberately small."""
    return f"""Current date/time:
- Local ({tz_name}): {local_str}
- UTC: {utc_str}

When resolving relative dates and times (e.g. "tomorrow", "next Friday", \
"in 2 minutes") for deadlines or reminders, compute the target moment using \
whichever of the two anchors above is more natural for the arithmetic \
(usually the local one for phrases like "tomorrow at 6pm", or the UTC one \
for "in N minutes/hours"). Then output an ISO-8601 datetime with an \
EXPLICIT offset: either reuse the local offset shown above verbatim (e.g. \
2026-07-10T18:00:00{now_local.strftime('%z')}) or convert fully to UTC and \
append 'Z' (e.g. 2026-07-10T10:00:00Z). Never output a datetime with no \
offset/Z, and never change the clock digits without also changing the \
offset to match -- the digits and the offset must describe the same \
absolute instant. You do not need to manually subtract the UTC offset \
yourself if you use the local anchor -- just attach the local offset shown \
above to your computed local time.
"""


def _clock() -> tuple[str, str, str, "datetime"]:
    tz_name = os.environ.get("TIMEZONE", "UTC")
    now_local = datetime.now(ZoneInfo(tz_name))
    now_utc = now_local.astimezone(timezone.utc)
    local_str = (
        now_local.strftime("%Y-%m-%dT%H:%M:%S%z")
        + f" ({now_local.strftime('%A')})"
    )
    utc_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return tz_name, local_str, utc_str, now_local


def build_system_blocks(chat_id: int, extra: str | None = None) -> list[dict]:
    """System prompt as cacheable blocks.

    Order matters: the static block must come FIRST, since a cache breakpoint
    covers everything before it. `extra` (used by the out-of-tool-calls
    synthesis pass) is appended as its own block rather than string-concatenated
    onto the prompt, so that pass still reads the same cached prefix.
    """
    tz_name, local_str, utc_str, now_local = _clock()
    blocks = [
        {"type": "text", "text": _STATIC_SYSTEM, "cache_control": _CACHE},
        {"type": "text", "text": _dynamic_system(tz_name, local_str, utc_str, now_local)},
    ]
    if extra:
        blocks.append({"type": "text", "text": extra})
    return blocks


def build_system_prompt(chat_id: int) -> str:
    """Flat-string form of the same prompt, for callers that aren't the
    tool loop (and for eyeballing what Claude actually sees)."""
    return "\n\n".join(b["text"] for b in build_system_blocks(chat_id))



_TRUNCATION_NUDGE = (
    "Your previous reply was cut off because it exceeded the length limit, so "
    "NONE of the tool calls in it ran -- nothing was saved. Try again, but "
    "issue at most 8 tool calls in this round and stop; you will get further "
    "rounds to finish the rest. If the request needs far more calls than that, "
    "say so plainly and propose a narrower scope instead of attempting it."
)

_OVER_BUDGET_REPLY = (
    "That request needs more work than I can fit into one go, so I've stopped "
    "rather than half-doing it -- nothing was saved. Try narrowing it (a "
    "single week, or one module at a time) and I'll take it from there."
)


def _blocks_to_dicts(content) -> list[dict]:
    """Convert an SDK response's content blocks into plain JSON-safe dicts.

    Needed because these turns are now kept in `history` (which lives in
    PTB's chat_data) and replayed on later requests -- storing SDK objects
    there would couple the stored history to one anthropic version. Unknown
    block types are dropped rather than guessed at.
    """
    out: list[dict] = []
    for block in content:
        if block.type == "text":
            if block.text:
                out.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
    return out


def _is_plain_user_turn(entry: dict) -> bool:
    return entry.get("role") == "user" and isinstance(entry.get("content"), str)


def _trim_history(history: list) -> None:
    """Trim to HISTORY_LIMIT entries WITHOUT orphaning a tool_result.

    A blunt `del history[:-N]` can now slice into the middle of a tool round
    and leave the list starting with a user turn full of tool_result blocks
    whose matching tool_use is gone -- the API rejects that outright. So after
    the length trim, drop from the front until the history starts on an
    ordinary text user turn again.
    """
    if len(history) > HISTORY_LIMIT:
        del history[: len(history) - HISTORY_LIMIT]
    while history and not _is_plain_user_turn(history[0]):
        del history[0]


def _mark_block(entry: dict) -> dict | None:
    """Copy a message with a cache breakpoint on its last content block.

    Empty content can't be cached, so those are left alone. Works on a copy
    so `history` keeps storing plain strings and never carries cache_control.
    """
    out = dict(entry)
    content = out["content"]
    content = (
        [{"type": "text", "text": content}]
        if isinstance(content, str)
        else [dict(b) for b in content]
    )
    if not content or not (content[-1].get("text", "x") or "").strip():
        return None
    content[-1] = {**content[-1], "cache_control": _CACHE}
    out["content"] = content
    return out


def _cache_messages(messages: list, turn_start: int) -> list:
    """Two message-level breakpoints: a fixed anchor plus a rolling one.

    The rolling breakpoint on the final message is what makes a multi-round
    turn cheap -- round N writes the prefix, round N+1 reads it and pays a
    write only on the delta.

    The anchor on this turn's incoming user message exists because of the
    20-block lookback limit. The lookback counts BLOCKS, not messages, and a
    bulk round can emit 8 tool_use blocks answered by 8 tool_result blocks --
    16 blocks in a single round. Two such rounds push the rolling breakpoint
    past the previous write's lookback window and the hit is lost. The anchor
    sits at a position that never moves for the whole turn, so a write
    accumulates there on round one and every later round can still fall back
    to it.

    Together with the tools and static-system breakpoints this uses all 4
    available slots.
    """
    if not messages:
        return messages
    out = list(messages)
    for idx in {turn_start, len(out) - 1}:
        if 0 <= idx < len(out):
            marked = _mark_block(out[idx])
            if marked is not None:
                out[idx] = marked
    return out


def _log_cache_usage(chat_id: int, calls: int, usage: dict) -> None:
    total = usage["write"] + usage["read"] + usage["fresh"]
    if not total:
        return
    logger.info(
        "chat %s: %d API call(s), input tokens -- cache read %d, cache write "
        "%d, uncached %d (%.0f%% served from cache)",
        chat_id, calls, usage["read"], usage["write"], usage["fresh"],
        100 * usage["read"] / total,
    )


async def get_response(
    chat_id: int,
    user_text: str,
    history: list,
    job_queue,
    conversation_id: str | None = None,
    turn_index: int = 0,
) -> str:
    """conversation_id/turn_index are eval instrumentation only. They default
    to None/0 so every existing caller keeps working unchanged -- when
    conversation_id is None nothing is recorded and this is the old function."""
    messages = history + [{"role": "user", "content": user_text}]
    # Index of this turn's incoming user message -- fixed for the whole turn,
    # so it works as a stable cache anchor as the tool rounds pile up.
    turn_start = len(history)
    system_blocks = build_system_blocks(chat_id)
    usage = {"read": 0, "write": 0, "fresh": 0}
    calls = 0
    api_ms = 0
    iterations = 0
    turn_started = time.perf_counter()
    if conversation_id is not None:
        database.record_turn_start(
            conversation_id, turn_index, chat_id, len(user_text), user_text
        )

    def _account(resp) -> None:
        u = getattr(resp, "usage", None)
        if u is None:
            return
        usage["read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        usage["fresh"] += getattr(u, "input_tokens", 0) or 0

    def _commit(
        final_text: str, keep_tool_turns: bool = True, terminated_by: str = "answer"
    ) -> str:
        """Fold this turn into `history` and return the reply.

        The whole turn is kept -- assistant tool_use rounds and their
        tool_result replies included -- not just the final text. Without
        those, a follow-up like "did that work?" has no record that 12 of 34
        reminders already got created, which is exactly how Kangani ended up
        confidently reporting reminders that never existed.
        """
        if keep_tool_turns:
            history.extend(messages[len(history):])
            # Every stored turn must end on an assistant reply. It won't when
            # the loop exited via the ceiling path, whose synthesis response
            # is deliberately never appended to `messages` (it's a one-off
            # call made with no tools).
            if not history or history[-1].get("role") != "assistant":
                history.append({"role": "assistant", "content": final_text})
        else:
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": final_text})
        _trim_history(history)
        _log_cache_usage(chat_id, calls, usage)
        if conversation_id is not None:
            database.finalize_turn(
                conversation_id,
                turn_index,
                terminated_by,
                iterations=iterations,
                api_calls=calls,
                api_ms=api_ms,
                total_ms=(time.perf_counter() - turn_started) * 1000,
                reply_text=final_text,
            )
        return final_text

    response = None
    hit_ceiling = True
    truncations = 0
    for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
        iterations = iteration
        _api_started = time.perf_counter()
        response = await _get_client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            tools=tools.TOOL_SCHEMAS_CACHED,
            messages=_cache_messages(messages, turn_start),
        )
        api_ms += (time.perf_counter() - _api_started) * 1000
        calls += 1
        _account(response)
        if response.stop_reason == "max_tokens":
            # The turn was cut off mid-generation, so its trailing tool_use
            # block has incomplete JSON input and MUST NOT be executed -- and
            # the turn as a whole can't be appended either, since an
            # unanswered tool_use poisons every later request. Drop it whole
            # and retry with an explicit smaller-batches instruction.
            truncations += 1
            logger.warning(
                "Response truncated at max_tokens for chat %s (attempt %d)",
                chat_id, truncations,
            )
            if truncations > MAX_TRUNCATION_RETRIES:
                return _commit(
                    _OVER_BUDGET_REPLY,
                    keep_tool_turns=False,
                    terminated_by="truncated",
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "(reply cut off)"}],
                }
            )
            messages.append({"role": "user", "content": _TRUNCATION_NUDGE})
            continue

        # An empty content list is rejected by the API on the next request,
        # so never store one.
        blocks = _blocks_to_dicts(response.content) or [
            {"type": "text", "text": "(no content)"}
        ]
        messages.append({"role": "assistant", "content": blocks})

        if response.stop_reason != "tool_use":
            hit_ceiling = False
            break

        tool_results = []
        call_index = 0
        for block in response.content:
            if block.type == "tool_use":
                # Timed around execute_tool only -- the handlers are synchronous
                # SQLite (plus Playwright for the image tools), so this is real
                # execution time, NOT the API round trip. The API round trip is
                # accumulated separately in api_ms; keeping them apart matters
                # because most handlers are sub-millisecond and would otherwise
                # be buried under ~2s of network in any latency chart.
                _tool_started = time.perf_counter()
                result_text, is_error = await tools.execute_tool(
                    block.name, block.input, chat_id, job_queue
                )
                if conversation_id is not None:
                    database.record_tool_call(
                        conversation_id,
                        turn_index,
                        block.name,
                        iteration,
                        call_index,
                        (time.perf_counter() - _tool_started) * 1000,
                        is_error,
                    )
                call_index += 1
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                        "is_error": is_error,
                    }
                )
        messages.append({"role": "user", "content": tool_results})

    if hit_ceiling:
        # Ran out of tool-call rounds (e.g. a combined calendar pulling
        # lessons+tasks+notes+reminders across several queries). Rather than
        # discard everything gathered so far, force ONE more call with no
        # tools available -- Claude has to synthesize a real answer from
        # whatever tool results already sit in `messages`, instead of the
        # user getting a generic "something went wrong" for a request that
        # mostly succeeded.
        logger.warning(
            "Hit MAX_TOOL_ITERATIONS for chat %s -- forcing a synthesis-only reply",
            chat_id,
        )
        _api_started = time.perf_counter()
        response = await _get_client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=build_system_blocks(
                chat_id,
                extra=(
                    "You are out of tool calls for this turn. Answer using "
                    "ONLY the tool results already gathered above -- do not "
                    "claim to call any more tools. If something is still "
                    "missing, say so briefly rather than guessing."
                ),
            ),
            messages=_cache_messages(messages, turn_start),
        )
        api_ms += (time.perf_counter() - _api_started) * 1000
        calls += 1
        _account(response)

    final_text = next(
        (b.text for b in response.content if b.type == "text"), ""
    ) or "Sorry, I couldn't come up with a response for that -- try rephrasing."

    return _commit(
        final_text, terminated_by="ceiling" if hit_ceiling else "answer"
    )
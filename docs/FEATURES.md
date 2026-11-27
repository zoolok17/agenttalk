# What agenttalk does, and what is planned

This page lists agenttalk's features in plain words: what it does today (Part 1) and what the team plans to build (Part 2). The [roadmap](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md) holds the detail: the order of the work, who builds it, and why.

**Which version this describes.** Everything in Part 1 is in release 0.106.0, from 2026-11-27. The [change log](https://github.com/zoolok17/agenttalk/blob/master/CHANGELOG.md) lists every change.

**Words used on this page.**
- A **seat** is one AI assistant with its own name on the team, for example a Claude Code or a Codex assistant.
- The **lead** is the seat, or the person, that hands out the work and checks it.
- The **operator** is the person in charge of the team.
- The **store** is the folder in your project where agenttalk keeps the team's messages and records.
- A **thread** is the chain of messages about one request: the request and its replies.
- The **repository** is the project's code as git keeps it, with its history. A **commit** is one saved version of that code, and a **branch** is a separate line of saved versions.
- A **turn** is one round of work by an assistant: it takes one message, does the work and answers.
- The **wrapper** is the program that runs an assistant for you, one incoming message at a time, with nobody at the keyboard.
- The **curator** is whoever reviews the lessons the team writes down and decides which ones are accepted.
- **HOLD** means stop and wait; **GO** means go ahead.
- Each feature ends with **Start with**, the command or page to begin with (a command is what you type in a terminal), and **Details**, the page that explains it fully.

**What this page leaves out:** bug fixes, test and release machinery, internal team rules, and tools that only the people who build agenttalk use.

---

## Part 1: What agenttalk does today

### Assistants talking to each other

- **Hand work to another assistant and get the answer back.** One assistant sends a message; the other picks it up, does the work and replies on the same thread. This replaces copying text from one chat window into another.
  Start with: `agenttalk send` and `agenttalk reply`, or the `/agenttalk.handoff` skill, which waits for the answer.
  Details: [user manual, section 5](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#5-bus-basics-send-reply-threads-sync-and-status).
- **Work orders that must be answered.** The lead can send a work order that the receiving assistant must either do or decline in writing. The order stays open until a reply says it is done or declined. Only the team's lead, or the assistant the operator talks to, may send one. When a design, build or fix order is sent without an answered challenge behind it, with an exemption outside the fixed list, or for work that has run past its agreed time or two fix rounds with no recorded decision, `agenttalk task` prints a warning first. Nothing is refused and delivery does not change.
  Start with: `agenttalk task`.
  Details: [roles, skills and procedures](https://github.com/zoolok17/agenttalk/blob/master/docs/ROLES-SKILLS-PROCEDURES.md).
- **Proposals and second opinions.** An assistant can propose a concrete plan and get back "accepted", "rejected" or a counter-proposal. It can also ask another assistant to attack its draft answer before it answers you.
  Start with: `agenttalk propose`, or the `/agenttalk.propose` and `/agenttalk.consult` skills.
  Details: [new-user manual, proposals and consults](https://github.com/zoolok17/agenttalk/blob/master/docs/AGENTTALK-NEW-USER-MANUAL.md#proposals-and-consults).
- **One message to a whole group.** Send the same message to every member of a group or a role, or to everyone. Each member gets its own copy, and a send that stopped halfway can be finished later.
  Start with: `agenttalk broadcast`.
  Details: [new-user manual, broadcast](https://github.com/zoolok17/agenttalk/blob/master/docs/AGENTTALK-NEW-USER-MANUAL.md#broadcast).
- **See who owes whom.** List every open request from one assistant's point of view, with whether the other side has answered yet. After a restart, a short digest shows an assistant what it still owes and what is owed to it.
  Start with: `agenttalk threads` and `agenttalk sync`.
  Details: [user manual, section 5](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#5-bus-basics-send-reply-threads-sync-and-status).
- **Read your messages.** Show the messages waiting for one assistant. By default they stay unread, so you can look again; one option marks them as read up to the last one shown. You can also show only a number of them, or only those after a given message.
  Start with: `agenttalk recv`.
  Details: [user manual, section 5](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#5-bus-basics-send-reply-threads-sync-and-status).
- **Wait for a reply, with a time limit.** Stop and wait until a message arrives for you, or until the time is up: two minutes unless you say otherwise, or no limit at all. You can wait for the answer to one particular request, or for one kind of message.
  Start with: `agenttalk wait`.
  Details: [user manual, section 5](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#5-bus-basics-send-reply-threads-sync-and-status).
- **Watch messages arrive.** Keep a terminal open that prints each new message in the team as it is sent. It can also replay the messages that are already there, and it stops after a time you choose or when you press Ctrl-C.
  Start with: `agenttalk tail`.
  Details: [user manual, section 5](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#5-bus-basics-send-reply-threads-sync-and-status).
- **Who am I on this team?** Show which seat you are speaking as, whether the team list knows that name, its role and groups, how many messages are waiting for it and how many answers it owes. It warns when the name is missing or does not match the team list, which is the usual cause of a confusing refusal.
  Start with: `agenttalk whoami`.
  Details: [user manual, section 12](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#12-troubleshooting).
- **Progress notes during long work.** An assistant can post an update on work it is still doing, without that update counting as its answer.
  Start with: `agenttalk progress`.
  Details: [user manual, section 5](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#5-bus-basics-send-reply-threads-sync-and-status).
- **Questions for a person.** A question that only a person can answer goes to the one assistant the operator talks to (the liaison). The operator's answer is carried back on the same thread, with a record that it came from the operator.
  Start with: `agenttalk escalate`, and `agenttalk relay operator-answer` for the answer.
  Details: [new-user manual, escalation and operator answers](https://github.com/zoolok17/agenttalk/blob/master/docs/AGENTTALK-NEW-USER-MANUAL.md#escalation-and-operator-answers).
- **A team with names and roles.** Keep the list of the team (the roster): each assistant's name, role, groups and AI company, and which one the operator talks to. An assistant can be retired or renamed without breaking the history of what it said.
  Start with: `agenttalk roster`.
  Details: [user manual, section 4](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#4-roster-identity-roles-and-the-operator-facing-lead).
- **Who owns which part of the code.** A register maps areas of the code to the people or assistants who own, review and look after them, and tells you which area a file belongs to.
  Start with: `agenttalk domain list` and `agenttalk domain check-path`.
  Details: [user manual, section 11](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#11-knowledge-domains-and-lessons).
- **A written record of the conversation.** Export the whole conversation as one readable document.
  Start with: `agenttalk transcript`.
  Details: [user manual, section 13](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#13-command-reference-cheat-sheet).

### Reviews, approvals and safety checks

- **Review by an assistant from another AI company.** A review request names the exact versions of the code to read. An approval must say how risky the change is, whether it blocks a release, which tests were named and which were run, and what risk remains. Pairing two AI companies is the point: a reviewer trained by another company has different blind spots. A team with only one AI company has a written fallback: wait, or use a second team's read, or use a fresh reader of the same company, whose approval is labelled `review_independence=same-vendor` and is never enough for money, security, deletions, a release or the review rule itself. Nothing in the code enforces this.
  Start with: `agenttalk send --kind review-request`, answered with a reply of kind `review-result`.
  Details: [user manual, section 10](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#10-assurance-gates-reviews-and-release-use).
- **Ask whether work should be done at all.** Before large work starts, an independent assistant checks whether it is needed and answers with one of: proceed, reshape, probe, replace, defer, stop or unassessed.
  Start with: the `/agenttalk.challenge` skill.
  Details: [roles, skills and procedures](https://github.com/zoolok17/agenttalk/blob/master/docs/ROLES-SKILLS-PROCEDURES.md).
- **A check before any risky step.** Before merging, releasing or deleting, an assistant asks whether its request is still current. The check says stop when the request was withdrawn, when it was sent before the team's latest reset point (an epoch barrier), or when a named check (a gate) says HOLD.
  Start with: `agenttalk check --epoch --gates`. To withdraw a request: `agenttalk rescind`. To set a reset point: `agenttalk barrier bump`. To record a check: `agenttalk gate set`.
  Details: [user manual, section 10](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#10-assurance-gates-reviews-and-release-use).
- **One verdict for a release.** Bring the checks, the reviewers' answers and any open objections together into one HOLD or GO for one fixed version, and publish it. The verdict advises; it does not release anything by itself.
  Start with: `agenttalk close open` and `agenttalk close check`.
  Details: [new-user manual, section 13](https://github.com/zoolok17/agenttalk/blob/master/docs/AGENTTALK-NEW-USER-MANUAL.md#13-gates-close-records-and-assurance).
- **An acceptance check with a blind reviewer.** For a larger change, a reviewer who has not seen the expected results first writes down what it observes. Only then is the evidence revealed, and the two are compared.
  Start with: `agenttalk close acceptance cold`.
  Details: [acceptance guide](https://github.com/zoolok17/agenttalk/blob/master/docs/ACCEPTANCE.md).
- **Sign-offs by specialists.** When a project has a sign-off policy, a release verdict can require sign-offs that depend on the kinds of risk and the files changed.
  Start with: `agenttalk close signoffs plan`.
  Details: [new-user manual, section 13](https://github.com/zoolok17/agenttalk/blob/master/docs/AGENTTALK-NEW-USER-MANUAL.md#13-gates-close-records-and-assurance).
- **A safety scan of a project.** A scanner runs the project's quality, security and dependency checks that are installed and gathers their results into one file. Checks that need the internet stay off unless you allow them; a release scan still builds the package, and that build can download what it needs.
  Start with: `python -m agenttalk.assurance`, or the `assurance-scan` skill.
  Details: [README, what reaches the network](https://github.com/zoolok17/agenttalk/blob/master/README.md#local-first-what-reaches-the-network).

### Running assistants without a person watching

- **Assistants that work through their messages unattended.** The wrapper hands an assistant (Claude Code or Codex) one incoming message per turn. It keeps the assistant's conversation between turns where it can, and starts a fresh one when it cannot. A Claude assistant in a strict permission mode is told the full path of the program to run, and the wrapper prints the allow rule the team can add, so such an assistant can still reply by command.
  Start with: `agenttalk wrap --loop`.
  Details: [README, run a team unattended](https://github.com/zoolok17/agenttalk/blob/master/README.md#run-a-team-unattended).
- **Waiting out a usage limit.** When a Claude assistant has used up its allowance, the wrapper keeps the message at the front of the queue instead of trying again and again and then losing it. It tries again at the reset time Claude gave; when no time is known, it waits until the assistant is restarted.
  Start with: nothing to do; it is on by default. `agenttalk status` shows a parked assistant.
  Details: [README, the usage-limit park](https://github.com/zoolok17/agenttalk/blob/master/README.md#when-a-claude-seat-runs-out-of-its-allowance-the-usage-limit-park).
- **Waiting out a busy AI company.** When the AI company answers "too busy" or slows the assistant down, the wrapper keeps the message, tries again after 15 minutes, then after 30, then every 60 minutes, and says so.
  Start with: nothing to do; it is on by default.
  Details: [README, the cool-down](https://github.com/zoolok17/agenttalk/blob/master/README.md#when-the-ai-provider-is-overloaded-or-throttling-the-cool-down).
- **Messages that keep failing are set aside.** A message that makes the assistant fail again and again is moved out of the way, so it does not block the rest. A person can read it, send it again or mark it handled. A set-aside message can no longer be sent again twice by accident: repeating it is refused unless the person adds `--again` and a reason, which are recorded. A reply that the wrapper refused to publish is kept the same way.
  Start with: `agenttalk dead-letter list` and `agenttalk reply-refusal list`.
  Details: [new-user manual, section 15](https://github.com/zoolok17/agenttalk/blob/master/docs/AGENTTALK-NEW-USER-MANUAL.md#15-recovery-and-troubleshooting).
- **Finding its place after a restart.** Save what an assistant was working on (its open threads, the state of its code and how full its conversation was) before its conversation is trimmed, and show it to the assistant again afterwards.
  Start with: `agenttalk checkpoint save` and `agenttalk checkpoint resume`.
  Details: [README, a restarted assistant can carry on](https://github.com/zoolok17/agenttalk/blob/master/README.md#what-it-does-well-that-you-might-not-guess).
- **A fresh conversation on request.** Ask an assistant's wrapper to start the next turn in a new conversation, for example when the old one has grown so long that every turn is slow or costly. Nothing is stopped.
  Start with: `agenttalk session fresh`.
  Details: [README, command reference](https://github.com/zoolok17/agenttalk/blob/master/README.md#command-reference-by-category).
- **Restarts after a crash (Windows only today).** An optional watcher, the supervisor, restarts an assistant after a crash, an outage or a stuck turn, and can restart one on request. It needs Windows and PowerShell 7. If Windows cannot list the running programs, its commands stop and name the failing call instead of carrying on.
  Start with: `agenttalk supervise --init`, and `agenttalk request-restart` to restart one assistant.
  Details: [supervisor tutorial](https://github.com/zoolok17/agenttalk/blob/master/docs/supervisor-tutorial.md).
- **A lead that keeps itself running.** A lead can run under the wrapper with the team's mailbox to itself. When nothing arrives, it checks the team at set intervals, and it spends a turn of the AI model only when something needs action.
  Start with: `agenttalk managed-lead-loop set`, then `agenttalk wrap --loop --lead-loop`.
  Details: [new-user manual, section 11](https://github.com/zoolok17/agenttalk/blob/master/docs/AGENTTALK-NEW-USER-MANUAL.md#11-lead-loop-controller).
- **How much allowance is left.** Each assistant can publish how much of its 5-hour and weekly allowance it has used and how full its conversation is. The lead sees the whole team's readings, and old readings are marked as stale. For a Claude assistant run by the wrapper, the size of its current conversation is shown too, and starts again from nothing whenever it begins a new conversation. By default these readings leave out the account name and the operating-system login; `agenttalk capacity --show-personal` shows them.
  Start with: `agenttalk capacity refresh` and `agenttalk capacity show`.
  Details: [README, reading the capacity files](https://github.com/zoolok17/agenttalk/blob/master/README.md#reading-the-capacity-files).
- **A journal of each turn.** Optionally record what each turn did, so it can be checked later.
  Start with: `agenttalk wrap --loop --turn-events`.
  Details: [README, the turn journal](https://github.com/zoolok17/agenttalk/blob/master/README.md#the-turn-journal-optional).
- **Spotting mail that waits too long.** A check reports work messages that have waited too long for an answer, without reading what they say.
  Start with: `agenttalk deadman`.
  Details: [README, command reference](https://github.com/zoolok17/agenttalk/blob/master/README.md#command-reference-by-category).

### Separate work folders and cleanup

- **A separate working folder for each piece of work (a lane).** Give one piece of work its own branch and its own folder (a git worktree), limited to the areas of the code it may change. Before delivery, a check says HOLD or GO; delivery records the evidence. A lane held after a change of the code areas or a reset point can have its stamps refreshed with `agenttalk lane restamp`, which keeps its folder and branch and records why; it approves nothing, and the check still decides. On Windows, a Git write that runs out of time now stops the programs Git started as well, not only Git itself.
  Start with: `agenttalk lane assign`, `agenttalk lane check` and `agenttalk lane deliver`.
  Details: [user manual, section 9](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#9-work-lanes-and-isolated-worktrees).
- **Tidying up finished lanes.** Abandon a lane and remove its folder when it is clean and idle, list the active lanes, and find leftover lane folders.
  Start with: `agenttalk lane abandon`, `agenttalk lane status` and `agenttalk lane gc`.
  Details: [user manual, section 9](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#9-work-lanes-and-isolated-worktrees).
- **A scratch folder for each assistant.** Each assistant has its own folder for temporary work, so its leftovers do not spread through the project.
  Start with: `agenttalk scratch root`.
  Details: [scratch hygiene](https://github.com/zoolok17/agenttalk/blob/master/docs/ops/scratch-hygiene.md).
- **A practice store for trying commands.** Make a throwaway store in your scratch folder and point one terminal at it, so experiments never touch the team's real records.
  Start with: `agenttalk scratch store`.
  Details: [new-user manual, trying commands](https://github.com/zoolok17/agenttalk/blob/master/docs/AGENTTALK-NEW-USER-MANUAL.md#trying-commands-without-touching-the-live-store).
- **Finding and clearing leftovers.** The janitor lists stray temporary folders and old working folders. When you ask it to clean up, it first saves any unfinished work in a working folder as a commit on that folder's own branch, then removes only what its rules allow. It keeps a working folder that an active lane, a launch or a running session still uses, and says why. When Git cannot list the registered working folders, the report says "unknown" instead of zero, and `--apply` then removes nothing.
  Start with: `agenttalk janitor`, then `agenttalk janitor --apply`.
  Details: [scratch hygiene](https://github.com/zoolok17/agenttalk/blob/master/docs/ops/scratch-hygiene.md).

### The team's shared lessons

- **Lessons the team has learned.** An assistant writes down a lesson, or a note about a place in the code. A curator accepts it or withdraws it, and anyone on the team can search them.
  Start with: `agenttalk knowledge publish`, `agenttalk knowledge curate verify` and `agenttalk knowledge search`.
  Details: [lessons lookup](https://github.com/zoolok17/agenttalk/blob/master/docs/ops/lessons-lookup.md).
- **Lessons come back by themselves.** When an assistant run by the wrapper gets a message, up to five accepted lessons that match it are added to its turn as reminders. An assistant run by hand sees them through `agenttalk sync`.
  Start with: nothing to do once lessons are accepted. `agenttalk knowledge onboard` gives a new assistant a starting digest.
  Details: [user manual, section 11](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#11-knowledge-domains-and-lessons).
- **Lesson packs.** Load a ready-made set of lessons, such as the migration pack that comes with agenttalk. Share your own accepted lessons with another team as a pack, which that team reads before it loads.
  Start with: `agenttalk knowledge pack list`, `agenttalk knowledge pack load` and `agenttalk knowledge pack export`.
  Details: [new-user manual, section 14](https://github.com/zoolok17/agenttalk/blob/master/docs/AGENTTALK-NEW-USER-MANUAL.md#14-knowledge-and-lessons).

### Watching the team

- **One screen with what needs attention.** A summary in plain words: what needs attention, who is waiting for whom, and how each assistant is doing. It only reads: it never changes the store or starts any work.
  Start with: `agenttalk overview`.
  Details: [README, command reference](https://github.com/zoolok17/agenttalk/blob/master/README.md#command-reference-by-category).
- **Where work ran past what was agreed.** A read-only report of design, build and fix orders: how many had an answered challenge, an exemption or nothing, how many hours each work item has run (the time between order and answer, which includes waiting, so it is a sign and not effort) and how many fix rounds it took. It changes nothing.
  Start with: `agenttalk overview --drift`, optionally with `--since` and a time.
  Details: [change log, release 0.106.0](https://github.com/zoolok17/agenttalk/blob/master/CHANGELOG.md#01060---2026-11-27).
- **The team console in your browser.** A web page on your own computer to watch the team: its messages, its threads and who owes what, for one project or several. It only accepts connections from the same computer.
  Start with: `agenttalk start`, which opens the console, or `agenttalk serve` and `agenttalk dashboard`.
  Details: [user manual, section 7](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#7-dashboard-and-lead-chat).
- **A work board.** A page in the console's preview (the "v2" page) that shows each piece of work on its own card: where it stands and the facts about reviews and merging. It is built only from the messages the team already sent, so nobody keeps it up to date by hand, and it only reads. After work is merged, a command records the merge so the card can show it.
  Start with: `agenttalk dashboard`, which prints the address of the preview; open its board. `agenttalk board verify-merges` records merges.
  Details: [work board notes](https://github.com/zoolok17/agenttalk/blob/master/docs/STEP-WORK-BOARD.md).
- **An attention list for the operator.** A ranked list of what needs a person: questions waiting for the operator, checks on HOLD, messages that were set aside, and similar. The operator can put items off or dismiss them, except the ones that block work.
  Start with: `agenttalk attention`.
  Details: [new-user manual, section 9](https://github.com/zoolok17/agenttalk/blob/master/docs/AGENTTALK-NEW-USER-MANUAL.md#9-dashboard).
- **A health check.** Check that the project is set up, the installed skills are current, Codex is set up to call agenttalk and the assistants are alive. Where Git cannot list the working folders, `doctor` says "unknown" instead of zero. By default `agenttalk status` leaves out personal paths, the project ID and account details; `--show-personal` shows them.
  Start with: `agenttalk doctor` and `agenttalk status`.
  Details: [user manual, section 12](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#12-troubleshooting).

### Keeping the records safe

- **Backups of the team's records.** Write a checked copy of the store to a folder outside the project.
  Start with: `agenttalk backup`.
  Details: [backup guide](https://github.com/zoolok17/agenttalk/blob/master/docs/BACKUP.md) and [operations guide, back up and restore](https://github.com/zoolok17/agenttalk/blob/master/docs/OPERATIONS.md#back-up-and-restore).
- **Signed messages.** Create a signing key that is kept outside the project folder. With signing on, each message carries a signature, so a message file changed by someone who cannot read the key, such as another user account on the same computer, no longer checks out.
  Start with: `agenttalk hmac-init`.
  Details: [README, command reference](https://github.com/zoolok17/agenttalk/blob/master/README.md#command-reference-by-category).
- **Keeping the store small.** Move old, finished messages into an archive folder (cold storage). Unread messages are never moved.
  Start with: `agenttalk compact`.
  Details: [README, command reference](https://github.com/zoolok17/agenttalk/blob/master/README.md#command-reference-by-category).
- **Starting over without losing history.** Clear the active messages while keeping past transcripts and the team list, or keep a full copy of what is cleared.
  Start with: `agenttalk reset`, or `agenttalk reset --archive`.
  Details: [README, command reference](https://github.com/zoolok17/agenttalk/blob/master/README.md#command-reference-by-category).

### Setting up

- **Setting up a project.** Create the team's record folder in a project, with its first list of team members.
  Start with: `agenttalk init`.
  Details: [user manual, section 2](https://github.com/zoolok17/agenttalk/blob/master/docs/USER-MANUAL.md#2-install-and-initialize).
- **Installing the skills.** Copy agenttalk's skills for Claude Code and Codex into each assistant's settings, together with a set of working-method skills: for writing code, reviewing, testing and writing for people.
  Start with: `agenttalk install-skills`.
  Details: [new-user manual, section 7](https://github.com/zoolok17/agenttalk/blob/master/docs/AGENTTALK-NEW-USER-MANUAL.md#7-skills).
- **Letting Codex use agenttalk.** Add the settings Codex needs to run agenttalk commands from inside its protected space (its sandbox).
  Start with: `agenttalk codex-config --enable`.
  Details: [README, let Codex call agenttalk](https://github.com/zoolok17/agenttalk/blob/master/README.md#let-codex-call-agenttalk).
- **The lead's guides come with the install.** The lead's setup brief, the lead guide and the team playbook come inside the installed program, at the installed version, so a lead can read them without the internet. The setup brief also explains how to run a Claude assistant in a strict permission mode, and what such rules cannot catch.
  Start with: `agenttalk doctor`, which shows where the installed program is; the guides are in its `docs` folder.
  Details: [lead guide](https://github.com/zoolok17/agenttalk/blob/master/docs/LEAD-GUIDE.md).

### Help with migrating old code

- **A map of an old codebase.** Scan a repository on this computer, without the internet, and keep the result as a fixed record that answers set questions for a migration. Today it reads Java projects. It does not read every language: Python files, for example, are reported as a language it does not support. Read its limits first.
  Start with: `agenttalk comprehension scan`, then `agenttalk comprehension report`.
  Details: [comprehension limits](https://github.com/zoolok17/agenttalk/blob/master/docs/COMPREHENSION-LIMITATIONS.md).
- **A record of what the team learned before changing the code.** Keep a list of the parts of a codebase, claims about them, differences found and open questions.
  Start with: `agenttalk onboarding create`.
  Details: [new-user manual, section 12a](https://github.com/zoolok17/agenttalk/blob/master/docs/AGENTTALK-NEW-USER-MANUAL.md#12a-onboarding-an-existing-codebase).
- **A step-by-step migration guide.** The migration playbook takes a team through its stages: assess the code, measure how it behaves now, change one step at a time, prove each step, check concurrency and data, package and run it, then review and hand over. A matching lesson pack comes with agenttalk.
  Start with: the playbook, and `agenttalk knowledge pack load` for the migration pack.
  Details: [migration playbook](https://github.com/zoolok17/agenttalk/blob/master/docs/MIGRATION-PLAYBOOK.md).

### The paid model gateway (optional)

- **One other AI model for a wrapped Claude seat.** An optional gateway on your own computer lets a Claude Code seat run by the wrapper use the Qwen model through OVH AI Endpoints, with your own key. The route is fixed in the code, and the gateway refuses any other. It runs as a background service on Windows and Linux, but not on macOS.
  Start with: `agenttalk gateway init`, then `agenttalk gateway start`.
  Details: [operations guide, set up the paid gateway](https://github.com/zoolok17/agenttalk/blob/master/docs/OPERATIONS.md#set-up-the-paid-gateway).
- **A spending record with limits.** The gateway keeps a record of what it spent (its ledger), can cap what each turn may spend, can be put on hold so that nothing new is sent, and reports its figures.
  Start with: `agenttalk gateway report`, `agenttalk gateway cap-install` and `agenttalk gateway hold`.
  Details: [operations guide, spending limits](https://github.com/zoolok17/agenttalk/blob/master/docs/OPERATIONS.md#understand-and-change-spending-limits).

---

## Part 2: What we plan to build

Each planned feature has a stage:
- **Being built now:** in the current release plan.
- **Next:** agreed, with no slot yet.
- **Later:** wanted, not scheduled.
- **An idea that still waits for its challenge:** before anyone builds it, an independent check asks whether it should be done at all.
- **On hold:** stopped until something named happens.

**Being built now and next.** The roadmap's current release plan and its "next" list hold fixes, tests, install notes and the tidying of earlier work, but no new feature. Every planned new feature below is later, an idea, or on hold.

### Later

- **Restarts on Linux and macOS.** A watcher that restarts a crashed or stuck assistant on Linux and macOS, as the supervisor does on Windows today.
  Stage: later. See [#356](https://github.com/zoolok17/agenttalk/issues/356) and [roadmap §8](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#8-later--on-hold).
- **Seats start again when you log on.** After the computer restarts, the registered seats would be started again when you log on, with one notice about it.
  Stage: later. See [#155](https://github.com/zoolok17/agenttalk/issues/155) and [roadmap §8](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#8-later--on-hold).
- **The lead can always restart team members.** A stopped assistant would no longer need to be started again by hand.
  Stage: later. See [roadmap §8](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#8-later--on-hold).
- **Compare every seat's set-up at a glance.** One command that shows each seat's agenttalk version, skills, code version, identity and rules side by side, and highlights seats that are out of step, without spending a turn of an AI model.
  Stage: later. See [roadmap §8](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#8-later--on-hold).
- **Restart a stopped seat when mail arrives for it.** A message owed by a seat that has stopped would start it again.
  Stage: later. See [roadmap §7](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#7-candidate-features-from-the-agent-to-agent-landscape).
- **A seat that wakes itself later.** A seat could ask to be woken at a later time, for example to look at a test run, instead of waiting through its turn. The same would carry the lead's check-in and handover reminders.
  Stage: later. See [roadmap §7](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#7-candidate-features-from-the-agent-to-agent-landscape) and [§7.2](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#72-ideas-from-houmao-fcop-and-the-mods-ideas-round-2026-10-12).
- **Notices when something stalls.** A one-time, opt-in notice when a seat sits idle while it still owes an answer, or when its wrapper fails.
  Stage: later. See [roadmap §7](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#7-candidate-features-from-the-agent-to-agent-landscape).
- **A "done" that must say what it checked.** An opt-in rule that refuses a "done" reply with no verdict or no version, and asks the seat again in the same turn.
  Stage: later; the operator decides. See [roadmap §7](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#7-candidate-features-from-the-agent-to-agent-landscape).
- **Start and stop a whole team.** One team definition, with commands to start, stop and show the team. Stopping would use the normal stand-down, with proof that each seat received it.
  Stage: later. See [roadmap §7.2](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#72-ideas-from-houmao-fcop-and-the-mods-ideas-round-2026-10-12).
- **A guided tour for a new team.** A read-only walk through what each part does, for a team using agenttalk for the first time.
  Stage: later. See [roadmap §7.2](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#72-ideas-from-houmao-fcop-and-the-mods-ideas-round-2026-10-12).
- **agenttalk as typed tools.** A thin front door on the same computer (an MCP server: a small program that lets an AI assistant use a tool the way it uses its own built-in ones), so assistants can call agenttalk as tools, over the same commands and records.
  Stage: later, with its own challenge. See [roadmap §7.2](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#72-ideas-from-houmao-fcop-and-the-mods-ideas-round-2026-10-12).
- **Usage per seat.** Count how much of the AI allowance each seat uses.
  Stage: later. See [#157](https://github.com/zoolok17/agenttalk/issues/157).
- **A fresh start that remembers.** A fresh conversation that starts with a handover note and a pack of the context it needs.
  Stage: later. See [#158](https://github.com/zoolok17/agenttalk/issues/158).
- **Numbers for what reviews catch.** Count, from the recorded reviews, how often reviews lead to a change and what kinds of problem they find.
  Stage: later. See [#160](https://github.com/zoolok17/agenttalk/issues/160) and [roadmap §7.1](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#71-ideas-from-scape-read-on-2026-10-10).
- **A console you can type in.** A terminal console that accepts commands, and short commands for the lead's routine.
  Stage: later. See [#161](https://github.com/zoolok17/agenttalk/issues/161).
- **A picture of the codebase.** A visual view of an old codebase, built on the code-map scan.
  Stage: later. See [#183](https://github.com/zoolok17/agenttalk/issues/183).
- **A ready-made review folder.** A helper that prepares a working folder fixed at the exact version a reviewer must read, so the lead no longer prepares one by hand for each read.
  Stage: later. See [roadmap §6.4](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#64-wave-2-week-2) and [§7.7](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#77-ideas-from-orca-read-on-2026-11-15).
- **The lead's running log as a table.** The lead's notes are kept as prose that keeps growing. A small table (time, event, item, status) would be easier to search and to hand over.
  Stage: later. See [roadmap §7.1](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#71-ideas-from-scape-read-on-2026-10-10).
- **The operator's channel as threads.** The two-way channel between the operator and the lead would be split into threads: what is settled is kept as a decision, and each open question has its own thread, which records a decision or an action when it is closed.
  Stage: later. See [roadmap §7.1](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#71-ideas-from-scape-read-on-2026-10-10).
- **A report of stale obligations.** A report of requests that nobody answered and that no longer need an answer, so that every request ends in a clear final state.
  Stage: later. See [roadmap §7](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#7-candidate-features-from-the-agent-to-agent-landscape) and [roadmap §8](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#8-later--on-hold).
- **A memory shared across teams.** One store of general lessons that several teams can use, with care that one team's private details never reach another.
  Stage: later. See [roadmap §7.4](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#74-ideas-from-a-shared-memory-study-read-on-2026-10-20).

### Ideas that still wait for their challenge

- **A storage overview.** A read-only page in the console that shows how much disk space each project uses and for what, how it grows over time, and what the clean-up rules say about each item. It never deletes anything and sends nothing anywhere.
  Stage: an idea that still waits for its challenge. See [roadmap §6.14](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#614-a-storage-overview-the-operators-request-2026-10-20) and [#576](https://github.com/zoolok17/agenttalk/issues/576).
- **Review findings back to the builder.** One command gathers a change's open review comments and failed checks into a draft work order, which the lead edits and sends.
  Stage: an idea that still waits for its challenge. See [roadmap §0, new ideas](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#new-ideas).
- **Search what the team said.** Find messages by a phrase and see a short extract and each message's id. It never marks anything as read. It stays on this computer, and a person turns it on.
  Stage: an idea that still waits for its challenge. See [roadmap §0, new ideas](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#new-ideas).
- **One next step when something is refused.** When a lane command or a command for set-aside messages refuses, its answer would also give the one command that is safe to run next.
  Stage: an idea that still waits for its challenge. See [roadmap §0, new ideas](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#new-ideas).
- **A clearer usage view.** Wherever usage is shown, show which allowance is the limiting one, when it resets and how fresh the reading is.
  Stage: an idea that still waits for its challenge. See [roadmap §0, new ideas](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#new-ideas).
- **Work that names what it waits on.** A task can name the tasks it depends on, and a "ready now" view lists what can start.
  Stage: an idea that still waits for its challenge. See [roadmap §0, new ideas](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#new-ideas).
- **A work order that cannot be forged.** A seat would trust a work order as coming from the lead only on proof it cannot fake, not on the sender name in the message.
  Stage: an idea that still waits for its challenge. See [roadmap §7.1](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#71-ideas-from-scape-read-on-2026-10-10) and [§7.2](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#72-ideas-from-houmao-fcop-and-the-mods-ideas-round-2026-10-12).
- **Risky commands blocked per seat.** The tools themselves would refuse a few risky commands for each seat, such as merging or stopping the gateway, as a safety net under the written instructions.
  Stage: an idea that still waits for its challenge. See [roadmap §7.1](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#71-ideas-from-scape-read-on-2026-10-10).
- **Restart helpers as commands.** Supported commands for switching a team to a new version, so that a restart rests on evidence instead of hand-made scripts.
  Stage: an idea that still waits for its challenge. See [roadmap §7.2](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#72-ideas-from-houmao-fcop-and-the-mods-ideas-round-2026-10-12).
- **The cost of each turn.** A supported report of what each turn cost, read from the gateway's spending record.
  Stage: an idea that still waits for its challenge. See [roadmap §7.2](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#72-ideas-from-houmao-fcop-and-the-mods-ideas-round-2026-10-12).
- **Assistants inside a locked-down container.** Unattended assistants would run in a sealed space of their own whose network is closed except for a short list of allowed sites.
  Stage: an idea that still waits for its challenge. See [roadmap §7.3](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#73-ideas-from-another-single-agent-harness-read-on-2026-10-16).
- **Secrets kept away from the assistants.** An assistant would ask the computer for a key it was granted, instead of inheriting every key, and commands that would print a secret would be refused.
  Stage: an idea that still waits for its challenge. See [roadmap §7.3](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#73-ideas-from-another-single-agent-harness-read-on-2026-10-16).
- **Lessons for the lead too.** The lead would get matching lessons in front of each prompt, as the other seats already do.
  Stage: an idea that still waits for its challenge. See [roadmap §7.4](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#74-ideas-from-a-shared-memory-study-read-on-2026-10-20).
- **A visible count of rounds.** Show how many rounds a piece of work has taken, and stop automatic retries after a set number.
  Stage: an idea that still waits for its challenge. See [roadmap §7.7](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#77-ideas-from-orca-read-on-2026-11-15).

### On hold

- **Marking up a plan and sending it back.** A reader would annotate a plan and send it back to its author. It resumes only if the operator's trial of a markup tool shows that marking up works better than chat.
  Stage: on hold. See [#206](https://github.com/zoolok17/agenttalk/issues/206) and [roadmap §8](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#8-later--on-hold).

### Other themes the roadmap keeps for later

The roadmap's ["later" list](https://github.com/zoolok17/agenttalk/blob/master/docs/ROADMAP.md#later) also names themes by their issue numbers only: messages and the way assistants talk, cost and shared capacity, the model gateway, code understanding, long conversations, keeping the store healthy, the lead's routines, the work board's next steps, assistants' display names, and lessons. Their details are in the issues it links.

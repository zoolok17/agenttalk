# The agenttalk charter

**In plain words:** this page says what agenttalk is trying to become: a software engineering organization whose workforce is AI agents, one that delivers software people can trust, with evidence, and spends only the effort each change needs. It is for anyone who uses, contributes to or decides about agenttalk. It describes a direction, not finished work: several of the guarantees it names are not in place yet, and it says which.

**Status:** agreed as the project's strategic direction on 18 November 2026, after an internal review: the lead's response and three independent challenges by agents of another vendor.

This is a statement of intent. It does not authorize any roadmap, code, infrastructure or team change by itself; each of those still needs its own decision. It was reviewed inside the project, not audited from outside.

## 1. Mission

agenttalk is an attempt to build a professional software engineering organization in software, with AI agents as its engineering workforce.

A good engineering organization needs much more than people who can write working code. It needs:
- knowledge and specialization;
- institutional memory;
- engineering standards;
- reviews, testing and independent verification;
- accountability;
- procedures that improve as the organization learns.

Human organizations often struggle to apply these consistently. Knowledge stays in people's heads, documentation goes stale, quality procedures are skipped under pressure, and expertise leaves with the people who had it.

agenttalk should not merely coordinate agents. It should provide the structure in which agents work as a disciplined, adaptable engineering organization that accumulates knowledge.

We are not trying to build the most sophisticated multi-agent framework. Agents, coordination, knowledge stores, procedures, QA tools and infrastructure exist to serve one goal: an organization that consistently delivers trustworthy software. They are not goals in themselves.

## 2. What we deliver

**The goal:** software that people can trust, with independently verifiable evidence of:
- what it does;
- how it was built;
- which requirements and constraints it meets.

This is not a promise of bug-free software. It is a process that:
- understands the requirements and makes uncertainties explicit;
- chooses a development and verification procedure that fits the change;
- implements the change;
- has its correctness, safety and quality evaluated independently;
- produces reproducible evidence tied to the delivered revision;
- records decisions, lessons and unresolved risks;
- refuses delivery when mandatory assurance conditions are not met, unless an authorized human explicitly accepts the exception.

We keep three things apart:
- **Functional correctness:** the software does what is required.
- **Engineering integrity:** it meets the agreed security, performance, architecture, maintainability and operational constraints.
- **Evidence integrity:** those claims are backed by reliable checks, tools and records.

Following a procedure is never the same as proving that a result is correct.

## 3. Two trust boundaries

agenttalk needs two separate boundaries.

**The outer boundary: execution security.** Agents work inside an isolated environment. They cannot reach:
- unrelated customer data;
- host credentials;
- systems outside their authorization.

**The inner boundary: engineering assurance.** Inside their permitted environment, agents still cannot change or bypass the checks that decide whether their own work is acceptable. For example, a building agent that edits a lint rule to hide a warning, or changes a test so that it accepts its own behaviour, must not be able to report "all checks passed".

The direction is to separate engineering execution from engineering assurance. An independent authority evaluates the delivered revision against policies and checks that the implementing agents cannot alter.

**Where we are today:**
- **Outer boundary:** being tried on experimental isolated machines.
- **Inner boundary:** not enforced. agenttalk's security model assumes agents that share one operating-system account cooperate (`SECURITY.md`), and a seat has been able to widen its own permissions through local settings (#823).

## 4. The minimum sufficient procedure, chosen by risk

A small interface change, a framework migration and a change to payment authorization should not go through the same process. We want the minimum sufficient engineering procedure for each change, not the maximum possible one.

**Risk.** It depends on:
- security sensitivity;
- data integrity;
- business consequences;
- architectural coupling;
- reversibility.

It does not depend simply on how many lines changed.

**Who decides the risk class.** Choosing the risk class is itself an engineering control:
- A human owner sets each project's mandatory floor.
- The project's pre-approved policy handles clearly low-risk changes automatically, without extra ceremony.
- Ambiguous classifications, elevated risk and exceptions need an independent decision.
- The implementing agent can never lower its own mandatory assurance.
- The classification decision is recorded against the revision that is delivered.

Agents may recommend procedural improvements. They may not get around mandatory controls because the controls are inconvenient.

## 5. Knowledge that outlives models and sessions

Knowledge lives outside the models. It should survive model changes, session compaction, agent retirement and team reorganization.

**Global knowledge** is reusable and portable across model vendors, for authorized teams:
- lessons;
- procedures;
- standards;
- known failure modes;
- verification strategies;
- migration methods.

**Project knowledge** belongs to one project:
- domain knowledge and architecture rules;
- requirements and decisions;
- conversations and review history;
- evidence;
- project lessons.

**History versus decisions.** Original records are preserved, and they are kept apart from current, authoritative decisions:
- an agent's suggestion is not an accepted requirement;
- an old decision may have been superseded;
- a claimed implementation may not have been completed.

We aim to trace code back to its requirements, decisions, conversations and checks. Where a link is unknown or only inferred, we say so.

**Confidentiality.** Client material stays separate from general knowledge. Privacy scrubbing alone does not make client-derived material safe or permissible to reuse. Promotion across clients needs explicit rules, authorization, provenance, and protection against both disclosure and knowledge poisoning. No agent can turn an unverified conclusion into organization-wide policy because another agent agreed with it.

**Honest status.** The knowledge architecture stays, but its benefit is not established. Automatic retrieval of relevant lessons has measured poorly ([#293, corrected report](https://github.com/zoolok17/agenttalk/issues/293#issuecomment-6926156373)). In one internal pilot, adding shared lessons to the automatically selected context produced no useful correction across 19 past decisions ([result](https://github.com/zoolok17/agenttalk/issues/293#issuecomment-6038377263)). Knowledge is judged by whether it improves future outcomes, not by how much of it we store. Its benefit needs its own controlled evaluation before we claim it.

## 6. Independent, verifiable quality assurance

We rely on established third-party tools rather than inventing every check ourselves:
- static analysis;
- security and dependency scanning;
- secret detection;
- unit, integration, contract and mutation tests;
- differential tests for migrations;
- performance tests;
- UI and real-device tests;
- independent audits, chosen by project and risk.

agenttalk already has a starting point for this:
- an assurance scan that can run Bandit, Semgrep, Gitleaks and dependency scanners;
- a development gate that binds its evidence to the source revision, manifest, plan and tool versions.

**What is missing is the independent authority behind the results.** It would establish that the evidence applies to the code actually delivered, that the required checks really ran, and that no agent could influence the evidence or its acceptance.

The agent that implements a change is never the only authority that certifies it. Telling an agent to run a check is not the same as enforcing it. Where practical, mandatory checks are machine-enforced gates, bound to the exact revision, tool versions and configuration. Agreement between several models is not proof of correctness. Each piece of evidence states what it supports and what remains unverified.

## 7. Overhead has to earn its cost

**The purpose of the engineering apparatus** is to reduce the probability and the consequences of defective delivery, not to generate activity or evidence. A review that catches a serious bug can be worth a great deal. A review round that changes nothing is a cost.

**What we optimize:** the minimum total expected cost, subject to acceptable quality and risk. The cost includes:
- model usage;
- coordination;
- infrastructure;
- human supervision;
- testing and rework;
- the expected cost of defects that escape.

There is therefore no universal cost threshold for removing a review. Extra verification may be worth little for a low-risk interface change, and a great deal for a payment migration.

We remove redundant ceremony and keep valuable safeguards:
- less status logging in the product's history, without losing the underlying records;
- no repeated broad reviews of trivial changes;
- focused verification where the impact of a change really is limited.

## 8. Untrusted input and isolation

Unknown repositories, build scripts, dependencies, documentation and agent messages are all untrusted input. Prompt instructions are not a security boundary.

**The direction:** separate the agents' development environment from the environment that runs untrusted builds and tests. The latter has no access to:
- model credentials;
- hidden evaluation material;
- unnecessary external services.

Dependencies come through controlled channels. Model calls pass through a trusted interface that applies the project's data-handling rules.

**Allowed endpoints are still exits:** an environment is not secure just because its agents lack administrator rights or its network is limited to a few destinations. Those destinations, such as the model providers and package servers, can still carry data out.

**Before any client code, containment tests are mandatory.** Deliberately hostile repositories attempt credential theft, forbidden network access, evidence tampering and impersonation.

## 9. Commercial direction

agenttalk is the open-source engineering coordination and organization framework.

**AgentTalkX** is the proposed engineering services company built on it. Its intended business is accountable engineering outcomes, not renting agents by the hour. Its first focus is legacy software modernization, because that problem allows objective verification.

The work comes in separately scoped layers:
1. **Upgrade** languages, frameworks, dependencies and runtimes while keeping agreed behaviour.
2. **Improve** the code, tests and maintainability.
3. **Modernize** the user experience.
4. **Extend** with new features and integrations.

Behaviour-preserving work is kept apart from behaviour changes. Each layer has reviewable milestones and acceptance criteria.

The first product is a managed engineering service, not a self-service platform. Its claim, that disciplined, independently verified engineering can be more consistent and economically attractive, will be measured before it is presented as proven.

**Requirements before any paid engagement:**
- commercial model-provider terms that allow client work, including a data-processing agreement and no training on client data;
- a client-isolated environment;
- a named human responsible for delivery and exceptions, and a customer acceptance owner;
- frozen behavioural baselines and a rehearsed rollback;
- priced discovery, rework and support;
- an incident process;
- retention and deletion rules that cover logs, backups and providers.

## 10. How we will know

Three questions are answered separately:

**A. Can AgentTalkX deliver reliable, profitable engineering services?** This needs:
- paid customer work;
- independently accepted deliverables;
- accounting for human effort and model cost;
- follow-up defect tracking.

**B. Does an agenttalk team improve delivery compared with simpler AI-assisted workflows?** We compare three set-ups:
- a single agent with human supervision;
- a single agent with an independent reviewer;
- an agenttalk team.

We compare them on quality, first-pass acceptance, escaped defects, total cost, elapsed time and human supervision.

**C. Does accumulated knowledge improve future work?** We run the same kinds of tasks with and without the applicable knowledge, and we keep "retrieval worked" apart from "the outcome improved".

**Limits we keep in view:**
- **Small experiments:** they can show differences in workflow behaviour, effort, verification and cost. They cannot prove an advantage in rare, serious defects. That needs larger samples and longer observation.
- **Field defect history:** it is reconstructed as distinct, classified defects (severity, affected release, how found, time to fix, user exposure), not counted from commits.
- **Engineering experiments versus customers:** a good result on a public codebase shows nothing about willingness to pay, contractual feasibility or production reliability.
- **Internal versus external review:** an internal review is valuable criticism, not an independent audit.

## 11. Decisions taken with this charter (18 November 2026)

| Proposal | Decision |
|---|---|
| Finish the current isolated-machine pilot | Approved |
| Measure the actual economics of assurance | Approved |
| Test containment with hostile repositories | Mandatory before any client code |
| Design independently enforced risk floors and acceptance gates | High priority |
| Simplify redundant status logging and routine coordination | Approved, with evidence preserved |
| Remove reviews on a cost criterion alone | Rejected |
| A token balancer and a remote lead-to-lead server now | Deferred |
| A full hosted platform | Deferred |
| One small benchmark as proof for or against agenttalk's quality advantage | Rejected |

## 12. The next milestone

A complete, small engineering engagement in which:
1. an autonomous team completes a defined change;
2. an independent authority applies the prescribed assurance requirements;
3. the team cannot manipulate the acceptance evidence;
4. the deliverable includes reproducible evidence, explicit limitations and measured engineering costs.

That would not prove commercial viability, or fewer defects than the alternatives. It would demonstrate the basic mechanism of the organization this charter describes, and it would be a credible foundation on which to test those questions.

## 13. What success looks like

- A new project can set up a capable team with relevant technical and domain knowledge.
- The team selects and adapts its procedures without unnecessary overhead.
- Validated knowledge accumulates and survives changes of model and of team.
- Client information stays protected and accessible only to authorized parties.
- Agents collaborate, specialize, review and recover from failures without depending on one irreplaceable conversation.
- Independent tools and checks provide meaningful, reproducible assurance.
- Customers can inspect the history and evidence behind every delivered change.
- Quality stays high while total engineering cost becomes competitive.
- People spend their involvement on intent, judgment, accountability and the decisions that really need them.

The engineering policies, project knowledge and acceptance criteria should not depend on which model happens to be doing the work. Any model, or any team, can be replaced while those stay in place.

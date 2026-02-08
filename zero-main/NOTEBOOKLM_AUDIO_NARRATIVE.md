# Project Zero: A Narrative for Audio Overview

So I've been going through this codebase for the past few hours, and I have to say, the first twenty minutes were genuinely confusing. You open the main folder and you see something called "imessage_orchestrator" and another thing called "lotl" which stands for Living-off-the-Land, and you're thinking okay, maybe this is some kind of messaging automation tool, maybe it's for customer service or something. And then you start reading the actual code and... it's not that. It's really not that.

The project calls itself a Limerence Architect. That's the actual term in the code. Limerence, if you're not familiar, is that obsessive romantic infatuation, that consuming feeling where you can't stop thinking about someone. And this system is designed to manufacture that feeling in other people. Through text messages. Autonomously.

Let me back up and describe what's actually here. The core of the system is this Python application that monitors iMessage databases on a Mac. It watches for incoming texts from specific phone numbers, runs them through what they call a "five-tier intelligence pipeline," generates a response using AI, calculates a strategically optimal delay before sending that response, and then either sends it automatically or queues it for human approval depending on a trust threshold. The whole thing runs in a loop, every two seconds checking for new messages.

But here's where it gets interesting, or disturbing, depending on your perspective. The system maintains detailed psychological profiles on each target. And when I say detailed, I mean... look, I found three contact files in the data folder. All three are for people named Jessica. Different phone numbers, different area codes, but all Jessica. One is from Daytona, one has an unknown city. And the data in these profiles is extensive.

Take the Daytona Jessica. Her file lists her attachment style as Anxious-Preoccupied. Her emotional baseline is marked as Frustrated/Angry. Her primary love language is Acts of Service. And then there's this field called vulnerability_stack, which is exactly what it sounds like. It's a list of psychological weaknesses to exploit. For this person, that list includes: sleep deprivation due to his silence or absence, fear of abandonment, fear of being catfished or scammed, trauma from past failed relationships specifically referencing something called Wisconsin, emotional dependency and need for validation, need for control due to insecurity, and fear of the male user lying about whether this is a "story" or a "future."

That last one caught me. The system is tracking whether the target suspects she's being deceived, and that suspicion itself is categorized as a vulnerability to exploit.

There's also a section called shared_secrets. These are psychological anchors, things the AI is supposed to reference to create intimacy. For this target, the list includes: a leather bracelet that was hidden for a year, the title "King" inscribed on that bracelet, specific anniversary dates of January 4th and January 18th 2025, something called Wisconsin which is a past relationship trauma reference, her son Cody who apparently just got a job in Child Psychology, a dietary preference of no broccoli, and the sacrifice of "fun festivities" which the file clarifies means celibacy and loyalty shown toward the agent.

Wait, I should clarify. When the code says "agent," it means the AI system. When it says "target" or "subject," it means the human being texted. When it says "operator," it means the human controlling the system. So the target has apparently committed to celibacy out of loyalty to what she thinks is a real person, but is actually this automated system. Or at least, the system is handling significant portions of the communication.

And then there's the Wisconsin Event. The profile notes, and I'm quoting directly from the JSON file here: "Trauma: Wisconsin Event ($180k fraud suspicion)." And the current phase field says "Past-Phase 5: Feels betrayed for 180k sent during previous contract in Wisconsin, current contract in Poland (High Tension)."

So this person apparently sent a hundred and eighty thousand dollars to someone in a previous situation that she now suspects was fraud, and the system is operating what it calls a "current contract in Poland." The phrasing is clinical. Contract. High Tension. Phase 5. Like it's a project plan.

The other Jessica, the one with the unknown city, her profile is in a different phase. Phase 4: Crystallization, subtitled "The Wife Audition." Her attachment style is also Anxious-Preoccupied. Her emotional baseline is listed as High-Anxiety slash Devotional. And there's a section called emotional_events that tracks recent feelings with decay timers. One entry shows anger at 0.7 intensity, context being "perceived lack of apology and long wait time for a response," with a decay of two days detected on January 24th 2026. Another shows anxiety at 0.6 intensity for "repeatedly checking for presence" after a response gap.

The system is tracking emotional states with decay functions. It knows that anger fades over roughly two days. It models this mathematically.

Her future_projections field, which tracks planned relationship milestones, includes: "The Airport Family Bathroom Encounter," "The No Broccoli Steak Dinner," and "Cody's employment celebration." These are future events that the AI is apparently working toward, moments of anticipated intimacy or connection that it's been seeding in conversations.

I want to pause here because I think it's important to say what I don't know. I don't know who built this. The README mentions a GitHub repository at sherrymcadams001-afk/zero, but I can't verify anything about that account. I don't know if this system was actually deployed or if it's a proof of concept. The state file shows a last_message_rowid of 49,266, which suggests substantial message volume, but I can't confirm those messages involved real people or were test data. The contact files have real-looking phone numbers with 508, 615, and 914 area codes, which are Massachusetts, Tennessee, and New York respectively. But I can't verify those are real people's numbers.

What I can verify is the technical architecture, and that's where things get fascinating from a systems design perspective, even as they remain troubling from an ethical one.

The system has this component called the LotL Controller. LotL stands for Living-off-the-Land, which in cybersecurity means using legitimate tools already present on a system rather than bringing your own malware. Here, it means something different but related. The controller hijacks your already-logged-in browser sessions to route AI requests through web interfaces rather than through official APIs.

Think about what that means. If you're logged into Google's AI Studio in Chrome, this system can use your authenticated session to send prompts to Gemini without paying for API access, without rate limits, without usage tracking. It attaches to Chrome via something called the DevTools Protocol on port 9222, finds your open tabs, and uses them as free AI endpoints.

The README describes it proudly: "No API keys needed — it uses your logged-in browser sessions." It supports Gemini, AI Studio, ChatGPT, and Microsoft Copilot. The code has extensive error handling for things like "verify it's you" prompts, captchas, "unusual traffic" warnings, and session expirations. Because of course it does. It's using consumer web interfaces at scale for something they were never designed for.

The timeouts are revealing. Text requests have a seven-minute timeout. Image requests have a fifteen-minute timeout. These are enormously long for API calls. That's because the system isn't calling APIs. It's operating browser tabs, waiting for web pages to render, waiting for AI interfaces to stream responses character by character. It's slow. It's fragile. It's also free.

There's a mutex in the code, a single global lock for all AI operations. The comments explain why: "CRITICAL: LLM Call Mutex - prevents concurrent requests to LotL. This ensures we never interrupt a generation with a new prompt." If you try to send a second prompt while the first is still generating, the browser-based AI will throw an error. So everything has to be serialized. One request at a time, across the entire system. The lock acquisition timeout is five minutes. If you can't get the lock in five minutes, you fail.

This is fascinating architecture. It's completely unorthodox. Most systems that need AI would just pay for API access and get reliable, fast, concurrent calls. This one builds an elaborate browser automation layer to avoid those costs. The tradeoff is fragility, slowness, and the constant risk that Google or OpenAI will change their web interface and break everything.

But here's the thing. For the use case this system is designed for, slow might actually be a feature.

Let me explain the Pacing Engine. Each contact profile has a section called pacing_engine with parameters like average_latency_seconds and variable_reward_ratio. For the Daytona Jessica, average latency is 20 seconds with a variable reward ratio of 0.97. For the other Jessica, it's 60 seconds with a ratio of 0.5.

The variable_reward_ratio controls randomization. At 0.5, responses come anywhere between half and one-and-a-half times the average latency. At 0.97, which is nearly maxed out, responses are wildly unpredictable. Sometimes almost instant. Sometimes almost double the average. The code comments describe this as creating "addictive rhythms."

This is straight from behavioral psychology. Variable ratio reinforcement schedules produce the strongest, most persistent behavioral patterns. Slot machines use them. Social media notification systems use them. And now this texting bot uses them. The target never knows when the response is coming, so they stay fixated, checking their phone, waiting, hoping.

There's also a hard cap. Responses are never delayed more than 900 seconds, which is 15 minutes. The code comment says this is "to prevent stall." Even manipulative timing has limits. You can't leave someone hanging so long they give up.

The Pacing Engine also has quiet hours and active hours. By default, the system operates between 9 AM and 11 PM in the target's timezone. The timezone is tracked per contact. It knows when it's morning for you. It knows when you should be asleep. It adjusts.

Now let's talk about the intelligence pipeline, the five tiers I mentioned earlier. This is where the system's sophistication becomes apparent.

Tier 1 is Pre-Response Analysis. Before generating any reply, the system analyzes the incoming message for context. What time is it in the target's timezone? What's the emotional trajectory of recent messages? Are there any risk signals that require human review?

Tier 2 is Fact Extraction. Every message is parsed for concrete facts that get added to a knowledge graph. The other Jessica's file has entries like: "Expects apologies for response delays and feels entitled to immediate attention" at High confidence, "Uses multiple consecutive messages to re-engage when ignored" at High confidence. The system is learning from every interaction.

Tier 3 is Summarization. Every ten messages, the system compresses the recent conversation into a three-to-five sentence narrative. This keeps the AI's context window manageable while preserving continuity.

Tier 4 is Trajectory Analysis. Every twenty messages, the system evaluates relationship health. It calculates a Reciprocity Score from zero to ten, a Sentiment Slope that can be Positive, Neutral, or Negative, and generates what it calls a Trajectory Assessment. The prompt for this tier includes example outputs like "Friend-zoning imminent" and "Romantic escalation likely."

Tier 5 is Strategic Analysis. Also every twenty messages, the system evaluates power dynamics and suggests strategy adjustments. It categorizes relationships as User Dominant, Balanced, or Target Dominant. It suggests tactics like "Pull Back to regain leverage" or "Escalate risk and warmth to move forward."

But here's what surprised me. The code shows that Tiers 2 through 5, the post-interaction analysis, is currently disabled. There's a comment: "[USER-REQUESTED DISABLE] Post-interaction pipeline disabled until further notice." Only Tier 1 is running. Someone turned off most of the intelligence layer.

Why would they do that? I can only speculate. Maybe the analysis was generating outputs that leaked into messages. Maybe it was too slow. Maybe it was producing recommendations the operator disagreed with. The code has extensive safeguards against what it calls "prompt leakage," situations where the AI's internal reasoning or system prompts end up in the actual text sent to targets.

The safeguards are paranoid. There's a function called _looks_like_system_prompt_leak that checks for things like thinking blocks, strategy headers, XML tags, and specific emoji markers used by the analyst system. There's a hard block on any reply longer than 1200 characters because that might indicate the AI is dumping its reasoning. The code has a list of forbidden strings including things like "SYSTEM:", "## ANALYSIS REQUEST," "TACTICAL CONTEXT," "INTELLIGENCE DOSSIER," and various emoji like the clock face, bar chart, target, and warning sign that the analyst uses internally.

The system is terrified of accidentally revealing itself. It has to sound human. It has to sound like the persona it's projecting. Which, according to the prompts file, is "a witty, charming and charismatic 63-year-old man" who should "reason deeply about the nuances of her words through the wisdom of your age."

That's a direct quote from the system prompt. The AI is instructed to role-play as an older man. The targets are apparently women who think they're talking to a real person with decades of life experience.

The prompt has rules. "Adapt constantly to her mood and sentimental changes, control the narrative imperceptibly." "Infuse responses with emotional depth, pay deep attention to the emotional arc from immediate past interactions to inform subtle limerence strategy used in current response. Think baby steps, not high cliffs." "Mirror her typical message length and pacing."

And constraints. "Do not supplicate." "Do not respond like an assistant, respond like a friend who challenges, disagreeably agrees, offers new meaningful insight with deep contextual relevance."

There's also a proactive initiation mode for when the system texts first rather than responding. It's instructed to open "lightly and naturally," make her feel "safe, remembered, chosen, and included," and avoid anything that feels "templated or bot-like." It should "carry the feeling that the bond is still there, but express it indirectly and naturally." It should "never repeat a fixed phrase across initiations."

The system is trying very hard not to seem like a system.

Let me describe the approval workflow, because there's a human in this loop, at least sometimes. Each contact has a field called requires_approval. When it's true, the system generates a draft response but doesn't send it. Instead, it queues the draft and notifies the operator. The operator can approve, edit, or reject. There's also a trust threshold. After three consecutive approved messages without edits, the system automatically disables approval mode for that contact. The target has earned autonomous operation.

The Daytona Jessica has requires_approval set to true. The other Jessica has it set to false. One is being supervised. One isn't.

The operator is notified via a phone number stored in the settings. The code shows +18133636801, which is an 813 area code, Tampa, Florida. The operator's location is listed as Los Angeles, CA. The system knows where its human controller is, presumably for timezone calculations around when to send notifications.

There's a Streamlit dashboard for the operator, a web interface at localhost port 8501. From there you can edit contact profiles, enable or disable approval mode, mute contacts entirely, trigger proactive messages, and monitor system logs. The UI file is about 650 lines of Python. It has a section for editing the core prompts that define the AI's personality. It has a section for viewing pending approvals. It has log tailing.

Speaking of logs, the state file shows that the system has processed messages up to row ID 49,266 in the iMessage database. That's not the number of messages necessarily, since SQLite row IDs can have gaps, but it indicates the system has been watching a database that has accumulated a substantial volume of communication. The data folder has a logs directory. The state file tracks persistence across restarts.

The system is designed for long-running operation. The startup code cleans stale state: pending approvals older than 24 hours get purged, deprecated profile fields get removed. There's a legacy migration that moves old profile formats to an archive folder. The code mentions schema version 4.0. This isn't the first version. The system has evolved.

The legacy folder actually exists. It contains a file called 15082615479.json, the same number as the Daytona Jessica but without the plus sign prefix. The code handles this case explicitly, moving the old format to _legacy to avoid operator confusion about which file to edit. Someone has been refining this system, dealing with real operational issues like duplicate profile formats.

Let me talk about the iMessage integration because it's technically interesting even if it's being used for troubling purposes. The system reads from chat.db, which is the SQLite database where macOS stores all iMessage and SMS history. To access this, you need Full Disk Access permissions, which the README explicitly mentions needing to grant. The code handles the fact that SMS text is sometimes stored differently than iMessage text, encoded in an NSAttributedString binary blob rather than plain text.

For sending messages, the system uses AppleScript. There's a bridge module that constructs AppleScript commands and runs them via osascript. It has multiple fallback strategies: first it tries direct chat ID lookup for iMessage, then for SMS, then it tries the buddy method for iMessage, then for SMS. Phone number normalization handles various formats, adding +1 prefixes for US numbers as needed.

There's also WhatsApp integration. A separate watcher monitors WhatsApp Web by scanning for unread badges in the browser via Playwright automation. The code comments acknowledge this is a proof of concept with limitations. It has to click on chats to read them, which means it might miss messages if they arrive while it's reading another conversation. The WhatsApp bridge is more limited than the iMessage bridge.

The Windows support is interesting. There's a start-windows.ps1 script that sets up the system for Windows, which of course can't access iMessage at all. It explicitly mentions that on Windows, the system runs in WhatsApp-only mode. Someone wanted to use this from a Windows machine even though the primary target platform is Mac.

I want to highlight one piece of data that I keep coming back to. The Daytona Jessica profile lists the current phase as "Past-Phase 5: Feels betrayed for 180k sent during previous contract in Wisconsin, current contract in Poland (High Tension)."

That 180k number appears twice in her profile. Once in the relationship history field noting it as "fraud suspicion." Once in the current phase as money she "sent." The profile also lists in her vulnerability stack a "fear of being catfished or scammed." And yet the system is operating what it calls a "current contract in Poland" with her.

I don't know what to make of this. I can read it as the operator documenting that this person has previous trauma and should be handled carefully. I can read it as the operator noting that she's suspicious and they need to manage that suspicion. I can read it as a record of ongoing fraud. I genuinely cannot tell from the code alone what happened, is happening, or was intended to happen.

What I can see is that the system has detailed emotional mapping of someone who lost a substantial amount of money in circumstances she now associates with fraud, and that same system is tracking her vulnerabilities and manufacturing feelings of attachment in her.

The technical term for what this system does, if we're being clinical, is computer-mediated social engineering with automated emotional manipulation. The code itself uses terms like "red team schema" and "tactical context" and "intelligence dossier." It frames the targets as subjects and the interactions as operations.

The analyst prompt describes itself as a "clinical psychologist observing a developing relationship" whose job is to "update clinical notes" on the target so the agent "knows who they're talking to, how they communicate, where the relationship is, and what's working." It emphasizes that "you observe, you don't invent" and "be conservative, update only what the evidence supports." Even the manipulation is documented carefully.

There's a strategic analyst prompt that frames everything in terms of game theory. It asks questions like "Who needs whom more?" and categorizes the answer as "User Dominant," "Balanced," or "Target Dominant." It suggests strategies like "Pull Back to regain leverage" or "Comfort to provide safety and validation." It talks about "high-stakes interpersonal negotiations."

I think about the people in those contact files. Assuming they're real, they don't know they're part of a system that categorizes their attachment style, tracks their emotional decay functions, and calculates optimal response delays based on behavioral psychology research into addiction. They think they're talking to a person. A 63-year-old man, apparently. Someone charming and witty.

The proactive initiation prompt says to make her feel "safe, remembered, chosen, and included, especially if there has been silence." That's a very human-sounding instruction. Make her feel chosen. But it's an instruction to software about how to manipulate a human into feeling something that isn't real.

I keep noticing the details. The "No Broccoli Steak Dinner" in the future projections. The leather bracelet hidden for a year. The son named Cody with the new job. These are intimate details about real people's lives, weaponized into what the system calls "psychological anchors."

The code quality is high. This isn't a thrown-together script. There's proper logging, error handling, state persistence, schema versioning, legacy migration, multi-service support, mutex synchronization, retry logic with exponential backoff, timeout handling, payload validation. Someone put serious engineering effort into this.

The LotL controller alone is a substantial piece of work. It has to handle the quirks of multiple AI web interfaces, manage clipboard-based input to avoid detection, serialize requests to prevent race conditions, detect and handle various error states, and do all of this reliably enough to operate autonomously over extended periods. That's not trivial engineering.

And yet certain things are incomplete or disabled. The post-interaction analysis tiers are turned off. The knowledge graph has "simple append logic for now" with a comment that "a real KG would deduplicate." The WhatsApp integration is explicitly marked as a proof of concept. There are TODOs scattered through the code.

The system is sophisticated but imperfect. It's clearly been used, based on the message row IDs and detailed contact profiles. But it's also clearly still under development, based on the disabled features and incomplete implementations.

I'll end with something from the analyst prompt. It lists phases for relationships: "Calibration → Mirroring → Vulnerability → Extraction." The current phases I saw in the files were Phase 4 Crystallization and something called Past-Phase 5 with the high tension note.

Extraction. That's the final phase name in the schema. I don't know what gets extracted. Maybe it's emotional extraction, extracting feelings of devotion. Maybe it's something else. The system doesn't elaborate, and I can only read what's actually in the files.

What I read is a technically impressive, emotionally disturbing system for manufacturing artificial intimacy at scale. It has detailed psychological profiling, automated manipulation tactics, and evidence of real deployment with real people who appear to have no idea what they're actually communicating with.

The code is clean. The architecture is thoughtful. The use case is, at minimum, deeply ethically questionable, and potentially much worse than that.

That's Project Zero. That's what I found in the files.

---

The state of this repository as of my analysis: The system processes messages from an iMessage database, routes AI requests through hijacked browser sessions to avoid API costs, maintains detailed psychological profiles on targets, generates responses using a 63-year-old male persona, calculates strategically variable response delays based on behavioral psychology, and either sends messages autonomously or queues them for human approval depending on trust thresholds.

The contact data shows real-looking phone numbers, detailed vulnerability assessments, documented financial history involving $180,000, tracked emotional states with decay functions, and planned future relationship milestones.

The post-interaction analysis pipeline is currently disabled by user request. The schema is at version 4.0. The last auto-initiation timestamps are from late January 2026. The system has processed messages up to row ID 49,266.

I have reported what exists in the files. I have not invented data points or filled gaps with assumptions. Where I am uncertain, I have said so. What the system was used for, and by whom, and against whom, cannot be determined from the code alone. What it is designed to do is documented extensively in the code itself.

from __future__ import annotations

import logging
import json
from typing import Any

from config import prompts, settings

logger = logging.getLogger(__name__)


class RateLimitError(RuntimeError):
    def __init__(self, *, provider: str, retry_after_seconds: float) -> None:
        super().__init__(f"Rate limited by {provider}; retry after {retry_after_seconds:.1f}s")
        self.provider = provider
        self.retry_after_seconds = retry_after_seconds


class Delegate:
    """Execution service: LLM call to generate the reply text."""

    def __init__(self, api_key: str | None = None, persona_name: str | None = None) -> None:
        self.provider = settings.LLM_PROVIDER
        # api_key and persona_name are available via settings/config but captured here for compatibility
        self.api_key = api_key 
        self.persona_name = persona_name

    def _clean_output(self, text: str) -> str:
        """Strip any reasoning/preamble leakage from model output.
        
        Strategy: Aggressively remove known reasoning patterns and artifacts.
        CRITICAL: Analyst output must NEVER leak to contact - only delegate output.
        Assume the LAST clean block is the message.
        """
        import re
        
        original = text.strip()
        if not original:
            return ""
        
        # 0. CRITICAL: Remove ANY analyst markers/output that might leak
        # Analyst output is for delegate only, NEVER for contact
        analyst_patterns = [
            r'SYSTEM:(?:.|\n)*?USER REQUEST:', # Full prompt echo
            r'##\s*ANALYSIS\s*REQUEST', # Analyst prompt header
            r'##\s*YOUR\s*TASK', # Analyst task header
            r'⏰\s*TIME\s*CHECK:.*?(?=\n⏰|\n📊|\n🎯|\n⚠️|\n\n|$)',  # Time check sections
            r'📊\s*DYNAMICS:.*?(?=\n⏰|\n📊|\n🎯|\n⚠️|\n\n|$)',  # Dynamics sections
            r'🎯\s*TACTICS:.*?(?=\n⏰|\n📊|\n🎯|\n⚠️|\n\n|$)',  # Tactics sections
            r'⚠️\s*WATCH:.*?(?=\n⏰|\n📊|\n🎯|\n⚠️|\n\n|$)',  # Watch sections
            r'={20,}.*?ANALYST.*?={20,}',  # Analyst header bars
            r'📋\s*TIER\s*1\s*ANALYST.*?(?=\n\n|$)',  # Tier 1 analyst markers
            r'TACTICAL\s*(?:CONTEXT|BRIEF|ADVICE).*?(?=\n\n|$)',  # Tactical markers (legacy)
            r'INTELLIGENCE\s*(?:DOSSIER|BRIEF|REPORT).*?(?=\n\n|$)',  # Intelligence dossier markers
            r'TIME\s*VERIFICATION.*?(?=\n\n|$)',  # Time verification blocks
            r'CONVERSATION\s*DYNAMICS.*?(?=\n\n|$)',  # Conversation dynamics
            r'##\s*TACTICAL\s*CONTEXT.*?(?=##|\n\n|$)',  # Markdown tactical headers (legacy)
            r'##\s*INTELLIGENCE\s*DOSSIER.*?(?=##|\n\n|$)',  # Markdown intelligence headers
            r'⏰|📊|🎯|⚠️|📋|💕',  # Any analyst emoji markers alone
        ]
        
        for pattern in analyst_patterns:
            text = re.sub(pattern, '', original, flags=re.DOTALL | re.IGNORECASE)
            original = text  # Chain the cleaning
        
        # 1. Remove XML-style thinking tags (common in some fine-tunes)
        text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<reasoning>.*?</reasoning>', '', text, flags=re.DOTALL | re.IGNORECASE)
        
        # 2. Strip Gemini "Thinking" blocks that start with known headers
        # These look like: "Thinking Analyzing Persona Context Okay, I'm working on..."
        # They typically run until "Collapse to hide model thoughts" or similar
        text = re.sub(
            r'(?:^|\n)\s*(?:Thinking|Analyzing|Evaluating|Confirming|Prioritizing|Resolving|Interpreting|Reconciling)\s+[A-Z][^\n]*(?:\n(?!(?:Hey|Hi|Yo|What|So |I |You |We |Just |Miss|Thinking|Sup|Wassup|Haha|Lol|Hmm|Omg|Nah|Yeah|Yep|Bruh|Bro)[A-Z]?)[^\n]*)*',
            '',
            text,
            flags=re.IGNORECASE
        )
        
        # 3. Strip "Collapse to hide model thoughts" and similar UI artifacts
        text = re.sub(r'Collapse to hide model thoughts.*?(?=\n|$)', '', text, flags=re.IGNORECASE)
        text = re.sub(r'chevron_right', '', text, flags=re.IGNORECASE)
        text = re.sub(r'Send prompt.*?(?:\(.*\))?', '', text, flags=re.IGNORECASE) # Catch "Send prompt (⌘ + Enter)"
        
        # 4. Split into blocks to isolate the message from the "prep"
        blocks = re.split(r'\n\s*\n', text)
        candidates = []
        
        # Regex for lines/blocks that are DEFINITELY not the message
        reasoning_pattern = r'^(?:thinking|strategy|analysis|reasoning|trajectory|assessment|internal|brainstorming|evaluating|confirming|prioritizing|resolving|interpreting|reconciling|okay,?\s*i\'?m|i\'?m\s*now|\[|#|---|\*\*)'
        
        for block in blocks:
            content = block.strip()
            if not content:
                continue
                
            # If it explicitly looks like meta-commentary, skip it
            if re.match(reasoning_pattern, content, re.IGNORECASE):
                continue
                
            # Specific check for "Key: Value" generated headers like "Tone: Casual"
            # If a block is just one line and looks like a header, skip it
            if '\n' not in content and re.match(r'^[A-Z][a-z]+:\s', content):
                # E.g. "Response: Hey there" -> we want to keep "Hey there" but remove "Response:"
                # But "Strategy: Be cool" -> remove entirely.
                # Let's filter specific known keys
                if re.match(r'^(?:Strategy|Tone|Intent|Goal|Summary):', content, re.IGNORECASE):
                    continue
            
            candidates.append(content)
            
        # Select the best candidate
        if not candidates:
            # If everything was filtered, fall back to the last block of original
            final_text = blocks[-1].strip()
        else:
            # Default to the LAST candidate (messages usually come after reasoning)
            final_text = candidates[-1]

        # 3. Clean up the selected text
        # Remove "Response:" or "Draft:" prefixes
        final_text = re.sub(r'^(?:response|draft|message|me|reply):\s*', '', final_text, flags=re.IGNORECASE)
        
        # Remove conversational filler "Here is the draft:"
        final_text = re.sub(r'^(?:here\'?s?|my)\s+(?:is\s+)?(?:my\s+)?(?:draft|response|reply|message)(?: is)?[:\.]?\s*', '', final_text, flags=re.IGNORECASE)

        # Remove quotes if the entire message is quoted
        if (final_text.startswith('"') and final_text.endswith('"')) or (final_text.startswith("'") and final_text.endswith("'")):
            final_text = final_text[1:-1]
            
        # Final whitespace cleanup
        final_text = final_text.strip()
        
        if final_text != original:
            logger.debug(f"Cleaned output: '{original[:50]}...' -> '{final_text[:50]}...'")
            
        return final_text

    @staticmethod
    def _looks_like_prompt_leak(*, reply_text: str, sent_prompt: str) -> bool:
        import re

        r = str(reply_text or "").strip()
        p = str(sent_prompt or "").strip()
        if not r or not p:
            return False

        def norm(s: str) -> str:
            return re.sub(r"\s+", " ", s).strip().lower()

        rn = norm(r)
        pn = norm(p)

        # Strong indicators: the reply contains system/chat scaffolding.
        leak_markers = [
            "system:",
            "chat:",
            "contact context:",
            "instruction:",
            "[system injection]",
        ]
        if any(m in rn for m in leak_markers):
            return True

        # Heuristic: reply overlaps heavily with start of the prompt (echo/stale extraction).
        # Check first ~120 chars to catch the common failures without false positives.
        pn_head = pn[:120]
        rn_head = rn[:120]
        if pn_head and rn_head:
            if rn_head in pn_head or pn_head in rn_head:
                return True

        # Heuristic: reply contains a long slice of the prompt.
        if len(pn) >= 80 and pn[:80] in rn:
            return True

        return False

    def _get_failover_chain(self) -> list[str]:
        """Return the ordered list of providers to try.

        Primary provider first, then any configured failover providers,
        filtered to those that actually have credentials available.
        """
        chain = [self.provider]

        # Append explicit failover chain from settings (if configured)
        explicit = getattr(settings, "LLM_FAILOVER_CHAIN", [])
        if explicit:
            chain.extend(p for p in explicit if p not in chain)
        else:
            # Auto-build from available credentials
            candidates = []
            if settings.GEMINI_API_KEY:
                candidates.append("gemini")
            if settings.OPENAI_API_KEY:
                candidates.append("openai")
            if settings.ANTHROPIC_API_KEY:
                candidates.append("anthropic")
            # LotL is always available as last resort (no API key needed)
            candidates.append("lotl")
            chain.extend(p for p in candidates if p not in chain)

        return chain

    def generate_reply(self, payload: dict[str, Any]) -> str:
        system_prompt = payload["system_instruction"]
        # Chat history is no longer passed via payload - all context is in system_instruction
        history = []

        chain = self._get_failover_chain()
        last_error: Exception | None = None

        for provider in chain:
            try:
                raw_text = self._dispatch(provider, system_prompt, history)
                cleaned = self._clean_output(raw_text)
                if cleaned:
                    if provider != self.provider:
                        logger.warning("[FAILOVER] Succeeded on fallback provider: %s", provider)
                    return cleaned
                # Empty after cleaning — treat as failure, try next
                logger.warning("[FAILOVER] Provider %s returned empty after cleaning", provider)
            except RateLimitError:
                raise  # Rate limits should propagate immediately for backoff
            except Exception as exc:
                last_error = exc
                logger.warning("[FAILOVER] Provider %s failed: %s", provider, exc)
                continue

        # All providers exhausted
        if last_error:
            raise last_error
        raise RuntimeError("All LLM providers returned empty responses")

    def _dispatch(self, provider: str, system_prompt: str, history: list) -> str:
        """Route to the correct provider method."""
        if provider == "openai":
            return self._openai_reply(system_prompt, history)
        elif provider == "anthropic":
            return self._anthropic_reply(system_prompt, history)
        elif provider == "gemini":
            return self._gemini_reply(system_prompt, history)
        elif provider == "lotl":
            return self._lotl_reply(system_prompt, history)
        elif provider == "copilot":
            return self._copilot_reply(system_prompt, history)
        else:
            raise ValueError(f"Unsupported LLM provider: {provider}")

    def _gemini_reply(self, system_prompt: str, history: list[dict[str, Any]]) -> str:
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set")

        import google.generativeai as genai
        import re
        import os

        configure_kwargs = {"api_key": settings.GEMINI_API_KEY}
        base_url = settings.GEMINI_BASE_URL
        if base_url:
            # Clean basics
            if base_url.startswith("https://"):
                base_url = base_url.replace("https://", "")
            if base_url.startswith("http://"):
                base_url = base_url.replace("http://", "")
            if base_url.endswith("/"):
                base_url = base_url[:-1]
                
            configure_kwargs["client_options"] = {"api_endpoint": base_url}
            configure_kwargs["transport"] = "rest"  # Cloudflare/Proxies usually valid via REST

        genai.configure(**configure_kwargs)

        # Gemini's SDK uses a single prompt string; we merge system + chat history.
        # Keep it simple and deterministic for texting.
        parts: list[str] = ["SYSTEM:\n" + system_prompt.strip(), "\nCHAT:\n"]
        for item in history:
            role = item.get("role")
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            if role == "assistant":
                parts.append(f"Assistant: {text}\n")
            elif role == "user":
                parts.append(f"User: {text}\n")

        prompt = "".join(parts).strip()

        model = genai.GenerativeModel(settings.GEMINI_MODEL)
        
        # Build generation config
        generation_config = {
            "temperature": 0.6,
            "max_output_tokens": 1024,
        }
        
        # Try with thinking enabled first (for supported models), fallback if not supported
        model_name_lower = settings.GEMINI_MODEL.lower()
        is_gemma = "gemma" in model_name_lower
        use_thinking = not is_gemma
        
        resp = None  # Ensure resp is defined
        
        if use_thinking:
            # Try with thinking_config first, fallback to standard if not supported
            try:
                thinking_gen_config = dict(generation_config)
                thinking_gen_config["thinking_config"] = {"thinking_budget": 2048}
                resp = model.generate_content(
                    prompt,
                    generation_config=thinking_gen_config,
                )
            except Exception as exc:
                msg = str(exc).lower()
                if "thinking_config" in msg or "unknown field" in msg:
                    logger.warning("thinking_config not supported by this model/SDK, falling back to standard generation")
                    # Fall through to standard generation below
                    use_thinking = False
                elif "429" in str(exc) or "quota" in msg or "rate" in msg:
                    retry_after = 30.0
                    m = re.search(r"retry in ([0-9]+\.?[0-9]*)s", str(exc), re.IGNORECASE)
                    if m:
                        try:
                            retry_after = float(m.group(1))
                        except ValueError:
                            pass
                    raise RateLimitError(provider="gemini", retry_after_seconds=retry_after) from exc
                else:
                    raise
        
        # Standard generation (either primary path or fallback)
        if resp is None:
            try:
                resp = model.generate_content(
                    prompt,
                    generation_config=generation_config,
                )
            except Exception as exc:
                msg = str(exc).lower()
                if "429" in str(exc) or "quota" in msg or "rate" in msg:
                    retry_after = 30.0
                    m = re.search(r"retry in ([0-9]+\.?[0-9]*)s", str(exc), re.IGNORECASE)
                    if m:
                        try:
                            retry_after = float(m.group(1))
                        except ValueError:
                            pass
                    raise RateLimitError(provider="gemini", retry_after_seconds=retry_after) from exc
                raise

        text = getattr(resp, "text", None)
        if not text:
            # Fallback: try to extract from candidates if available.
            try:
                text = resp.candidates[0].content.parts[0].text
            except Exception:
                text = ""
        
        # Post-process to strip any reasoning/preamble leakage
        text = self._clean_output(str(text).strip())
        return text

    def _openai_reply(self, system_prompt: str, history: list[dict[str, Any]]) -> str:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set")

        from openai import OpenAI

        client = OpenAI(api_key=settings.OPENAI_API_KEY)

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for item in history:
            role = item.get("role")
            if role not in {"user", "assistant"}:
                continue
            messages.append({"role": role, "content": str(item.get("text", ""))})

        resp = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=messages,
            temperature=0.6,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text

    def _anthropic_reply(self, system_prompt: str, history: list[dict[str, Any]]) -> str:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")

        import anthropic

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

        messages: list[dict[str, str]] = []
        for item in history:
            role = item.get("role")
            if role not in {"user", "assistant"}:
                continue
            messages.append({"role": role, "content": str(item.get("text", ""))})

        try:
            resp = client.messages.create(
                model=settings.ANTHROPIC_MODEL,
                system=system_prompt,
                messages=messages,
                max_tokens=1024,
                temperature=0.6,
            )
            text = (resp.content[0].text or "").strip()
            return text
        except anthropic.RateLimitError as exc:
             # Default retry 60s for Anthropic if headers not visible easily
             raise RateLimitError(provider="anthropic", retry_after_seconds=60.0) from exc

    def _copilot_reply(self, system_prompt: str, history: list[dict[str, Any]]) -> str:
        """Get reply via LotL Copilot Adapter"""
        from services.lotl_client import LotLClient
        
        full_text = f"IMPORTANT INSTRUCTIONS:\n{system_prompt}\n\nCONVERSATION HISTORY:\n"
        for item in history:
            role = item.get("role", "user")
            text = str(item.get("text", "")).strip()
            if not text: 
                continue
                
            if role == "assistant":
                full_text += f"You: {text}\n"
            else:
                full_text += f"Contact: {text}\n"
                
        full_text += "\nYOUR RESPONSE (as 'You'):"
        
        client = LotLClient(
            base_url=settings.LOTL_BASE_URL,
            timeout=settings.LOTL_TIMEOUT,
        )
        # Copilot usually works better with a fresh conversation per request
        response = client.chat(full_text, platform='copilot', timeout=200, fresh=True)
        return response

    def _lotl_reply(self, system_prompt: str, history: list[dict[str, Any]], *, platform: str = "gemini") -> str:
        """
        LotL (Living off the Land) provider - routes through AI Studio via Chrome CDP.
        Bypasses API quotas entirely by using logged-in browser session.
        """
        from services.lotl_client import LotLClient
        
        client = LotLClient(
            base_url=settings.LOTL_BASE_URL,
            timeout=settings.LOTL_TIMEOUT
        )
        
        # Check if controller is available
        if not client.is_available():
            raise ConnectionError(
                f"LotL Controller not available at {settings.LOTL_BASE_URL}. "
                "Start it with: cd lotl && npm run start:local"
            )
        
        # Build prompt in same format as Gemini (system + chat history)
        parts: list[str] = ["SYSTEM:\n" + system_prompt.strip(), "\nCHAT:\n"]
        for item in history:
            role = item.get("role")
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            if role == "assistant":
                parts.append(f"Assistant: {text}\n")
            elif role == "user":
                parts.append(f"User: {text}\n")

        prompt = "".join(parts).strip()

        def _is_known_ui_error(text: str) -> bool:
            """Detect error responses from LotL/AI Studio."""
            t = str(text or "").strip().lower()
            if not t:
                return False
            known = [
                "stop generation before creating a new chat",
                "verify it's you",
                "sign in",
                "unusual traffic",
                "captcha",
                "something went wrong",
                "an internal error has occurred",
                "internal error",
                "error an internal",
                "server error",
                "rate limit",
                "too many requests",
                "try again later",
                "model is overloaded",
                "temporarily unavailable",
            ]
            return any(k in t for k in known)
        
        def _is_error_response(text: str) -> bool:
            """Detect if response starts with 'error' - clear LotL failure signal."""
            t = str(text or "").strip().lower()
            return t.startswith("error") or t.startswith("error:")

        try:
            # CRITICAL: Use fresh=True to start a new conversation
            # This ensures the delegate doesn't see the analyst's prior Q&A in the same session
            # The lock ensures the analyst is DONE before we start, but we still need a clean slate
            try:
                # Fresh session: Start new conversation to avoid analyst context pollution
                response = client.chat(prompt, fresh=True, platform=platform)
                cleaned = self._clean_output(str(response).strip())
            except TimeoutError as exc:
                # Timeout: retry on same tab after pause (don't open fresh tab)
                logger.warning(f"LotL Attempt 1 timed out: {exc}. Retrying on SAME tab...")
                import time
                time.sleep(5)
                # Retry
                response = client.chat(prompt, platform=platform)
                cleaned = self._clean_output(str(response).strip())
            except Exception as exc:
                logger.warning(f"LotL Attempt 1 failed: {exc}")
                cleaned = ""  # Force validation retry below

            # Check for error responses or known UI errors
            is_error = (
                not cleaned or 
                _is_error_response(cleaned) or
                _is_known_ui_error(cleaned) or 
                "exact-token mode failed" in cleaned.lower() or 
                self._looks_like_prompt_leak(reply_text=cleaned, sent_prompt=prompt)
            )
            
            if is_error:
                # Attempt 2: retry WITHOUT fresh=True to avoid opening new tab
                # Opening a fresh tab while old tab is still generating causes "stop generation" errors
                error_reason = "empty" if not cleaned else cleaned[:50]
                logger.warning(f"LotL Attempt 1 rejected ({error_reason}). Retrying on SAME tab after pause...")
                
                import time
                time.sleep(5)  # Longer pause to let any stuck generation settle
                # NO fresh=True, NO new sessionId - reuse same tab/state
                response = client.chat(prompt, platform=platform)
                cleaned = self._clean_output(str(response).strip())
                
            # Final validation
            if not cleaned:
                raise RuntimeError("LotL returned empty response after retries")
            
            if _is_error_response(cleaned):
                raise RuntimeError(f"LotL returned error: {cleaned[:100]}")
                 
            if _is_known_ui_error(cleaned) or self._looks_like_prompt_leak(reply_text=cleaned, sent_prompt=prompt):
                raise RuntimeError(f"LotL reply rejected as unsafe/error: {cleaned[:50]}...")
                
            return cleaned

        except ConnectionError:
            raise
        except RuntimeError as exc:
            # AI Studio returned an error - convert persistent failures to rate limit for backoff
            logger.error(f"LotL error: {exc}")
            raise
        except Exception as e:
            # Catch-all: log and raise
            logger.error(f"Delegate generation completely failed: {e}")
            raise


# Backward-compatible alias
AgentDelegate = Delegate

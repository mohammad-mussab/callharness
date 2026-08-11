"""Example: Pipecat voice bot with CallHarness analytics.

This is a standard Pipecat cascading bot (Deepgram STT -> OpenAI LLM ->
Cartesia TTS) with CallHarness added. The CallHarness-specific lines are marked
with `# <-- CallHarness`.

Requires: pip install pipecat-ai[daily,deepgram,openai,cartesia] callharness-sdk
"""

import asyncio
import os

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.transcript_processor import TranscriptProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport

from callharness_sdk.pipecat import CallHarnessMetricsObserver, create_recorder  # <-- CallHarness

SYSTEM_PROMPT = "You are a friendly receptionist for Brightsmile Dental. Keep answers short."


async def main():
    transport = DailyTransport(
        os.environ["DAILY_ROOM_URL"],
        None,
        "Receptionist",
        DailyParams(audio_in_enabled=True, audio_out_enabled=True, vad_analyzer=SileroVADAnalyzer()),
    )

    stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])
    llm = OpenAILLMService(api_key=os.environ["OPENAI_API_KEY"], model="gpt-4o-mini")
    tts = CartesiaTTSService(api_key=os.environ["CARTESIA_API_KEY"])

    context = OpenAILLMContext([{"role": "system", "content": SYSTEM_PROMPT}])
    context_aggregator = llm.create_context_aggregator(context)

    transcript = TranscriptProcessor()

    recorder = create_recorder(                                    # <-- CallHarness
        base_url=os.environ.get("CALLHARNESS_URL", "http://localhost:8010"),
        api_key=os.environ.get("CALLHARNESS_API_KEY"),
        agent_id="dental-receptionist",
    )
    recorder.attach(transcript)                                    # <-- CallHarness

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            transcript.user(),
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            transcript.assistant(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True, enable_metrics=True),
        observers=[CallHarnessMetricsObserver(recorder)],           # <-- CallHarness
    )

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        await task.cancel()

    runner = PipelineRunner()
    await runner.run(task)

    await recorder.flush(end_reason="completed")                   # <-- CallHarness


if __name__ == "__main__":
    asyncio.run(main())

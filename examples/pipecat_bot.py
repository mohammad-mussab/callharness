"""Example: Pipecat voice bot with OpenCall analytics.

This is a standard Pipecat cascading bot (Deepgram STT -> OpenAI LLM ->
Cartesia TTS) with OpenCall added. The OpenCall-specific lines are marked
with `# <-- OpenCall`.

Requires: pip install pipecat-ai[daily,deepgram,openai,cartesia] opencall-sdk
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

from opencall_sdk.pipecat import create_recorder  # <-- OpenCall

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

    recorder = create_recorder(                                    # <-- OpenCall
        base_url=os.environ.get("OPENCALL_URL", "http://localhost:8010"),
        api_key=os.environ.get("OPENCALL_API_KEY"),
        agent_id="dental-receptionist",
    )
    recorder.attach(transcript)                                    # <-- OpenCall

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

    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        await task.cancel()

    runner = PipelineRunner()
    await runner.run(task)

    await recorder.flush(end_reason="completed")                   # <-- OpenCall


if __name__ == "__main__":
    asyncio.run(main())

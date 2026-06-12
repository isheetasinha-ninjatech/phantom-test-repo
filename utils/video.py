"""
Video Generation Utilities
===========================

Generate videos from text prompts using OpenAI Sora 2 / Sora 2 Pro.

Supported models:
    - sora (openai/openai/sora-2) — Standard quality, faster
    - sora-pro (openai/openai/sora-2-pro) — Higher quality, slower

Valid sizes: "1280x720" (landscape), "720x1280" (portrait)
Max duration: 8 seconds

The video generation workflow is asynchronous:
    1. Submit a generation request → get a video_id
    2. Poll for status until "completed"
    3. Download the video content

Usage:
    from utils.video import generate_video, submit_video, poll_video, download_video

    # All-in-one (submit, poll, download)
    path = generate_video("A cat playing with yarn in a garden")

    # With options
    path = generate_video(
        prompt="A drone shot over a mountain lake at sunset",
        model="sora-pro",
        size="1280x720",
        seconds=8,
        output="drone_shot.mp4",
    )

    # Step-by-step for more control
    video_id = submit_video("A bouncing ball")
    status = poll_video(video_id)  # blocks until done
    path = download_video(video_id, output="ball.mp4")
"""

import time
from pathlib import Path

import requests
from utils.litellm_client import api_url, get_headers, resolve_model

# Valid sizes for video generation
VALID_SIZES = ["1280x720", "720x1280"]

# Default model for video generation
DEFAULT_VIDEO_MODEL = "sora"


def submit_video(
    prompt: str,
    model: str = DEFAULT_VIDEO_MODEL,
    size: str = "1280x720",
    seconds: int = 8,
    timeout: int = 60,
) -> str:
    """
    Submit a video generation request (non-blocking).

    Args:
        prompt:  Text description of the desired video.
        model:   Model alias or full ID. Options: "sora", "sora-pro".
        size:    Video dimensions. "1280x720" (landscape) or "720x1280" (portrait).
        seconds: Video duration in seconds (max 8).
        timeout: Request timeout in seconds.

    Returns:
        The video_id string for polling and download.

    Raises:
        ValueError: If size is not valid or seconds > 8.
        RuntimeError: If the API returns an error.

    Example:
        >>> video_id = submit_video("A sunset timelapse")
        >>> print(video_id)
        'video_abc123...'
    """
    if size not in VALID_SIZES:
        raise ValueError(f"Invalid size '{size}'. Must be one of: {VALID_SIZES}")
    if seconds > 8:
        raise ValueError(f"Max duration is 8 seconds, got {seconds}")

    model_id = resolve_model(model)

    payload = {
        "model": model_id,
        "prompt": prompt,
        "seconds": str(seconds),
        "size": size,
    }

    r = requests.post(
        api_url("/v1/videos"),
        headers=get_headers(),
        json=payload,
        timeout=timeout,
    )

    if r.status_code != 200:
        error = r.json().get("error", {}).get("message", r.text[:300])
        raise RuntimeError(f"Video submission failed ({r.status_code}): {error}")

    data = r.json()
    video_id = data.get("id")
    if not video_id:
        raise RuntimeError(f"No video ID in response: {data}")

    return video_id


def check_video_status(video_id: str, timeout: int = 30) -> dict:
    """
    Check the status of a video generation job.

    Args:
        video_id: The video ID returned by submit_video().
        timeout:  Request timeout in seconds.

    Returns:
        Dict with keys: status, progress, and other metadata.
        Status values: "queued", "in_progress", "completed", "failed".

    Example:
        >>> info = check_video_status("video_abc123")
        >>> print(info["status"])
        'in_progress'
    """
    r = requests.get(
        api_url(f"/v1/videos/{video_id}"),
        headers=get_headers({"custom-llm-provider": "openai"}),
        timeout=timeout,
    )

    if r.status_code != 200:
        raise RuntimeError(f"Status check failed ({r.status_code}): {r.text[:300]}")

    return r.json()


def poll_video(
    video_id: str,
    interval: int = 5,
    max_wait: int = 300,
    verbose: bool = True,
) -> str:
    """
    Poll until a video generation job completes.

    Args:
        video_id: The video ID returned by submit_video().
        interval: Seconds between polls.
        max_wait: Maximum total wait time in seconds.
        verbose:  Print status updates.

    Returns:
        Final status string ("completed" or "failed").

    Raises:
        TimeoutError: If max_wait is exceeded.
        RuntimeError: If the video generation fails.

    Example:
        >>> status = poll_video("video_abc123")
        'completed'
    """
    elapsed = 0
    poll_num = 0

    while elapsed < max_wait:
        time.sleep(interval)
        elapsed += interval
        poll_num += 1

        info = check_video_status(video_id)
        status = info.get("status", "unknown")
        progress = info.get("progress", 0)

        if verbose:
            print(f"  Poll {poll_num}: {status} (progress: {progress}%)")

        if status == "completed":
            return status
        elif status == "failed":
            error = info.get("error", "Unknown error")
            raise RuntimeError(f"Video generation failed: {error}")

    raise TimeoutError(f"Video generation timed out after {max_wait}s")


def download_video(
    video_id: str,
    output: str = "generated_video.mp4",
    timeout: int = 120,
) -> str:
    """
    Download a completed video.

    Args:
        video_id: The video ID of a completed generation.
        output:   Output file path.
        timeout:  Request timeout in seconds.

    Returns:
        Path to the saved video file.

    Raises:
        RuntimeError: If the download fails.

    Example:
        >>> path = download_video("video_abc123", output="my_video.mp4")
        'my_video.mp4'
    """
    r = requests.get(
        api_url(f"/v1/videos/{video_id}/content"),
        headers=get_headers({"custom-llm-provider": "openai"}),
        timeout=timeout,
    )

    if r.status_code != 200:
        raise RuntimeError(f"Video download failed ({r.status_code}): {r.text[:300]}")

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_bytes(r.content)
    return output


def generate_video(
    prompt: str,
    model: str = DEFAULT_VIDEO_MODEL,
    size: str = "1280x720",
    seconds: int = 8,
    output: str = "generated_video.mp4",
    max_wait: int = 300,
    verbose: bool = True,
) -> str:
    """
    Generate a video end-to-end: submit, poll, and download.

    This is the main convenience function that handles the full workflow.

    Args:
        prompt:   Text description of the desired video.
        model:    Model alias or full ID. Options: "sora", "sora-pro".
        size:     "1280x720" (landscape) or "720x1280" (portrait).
        seconds:  Video duration (max 8).
        output:   Output file path.
        max_wait: Maximum wait time for generation in seconds.
        verbose:  Print progress updates.

    Returns:
        Path to the saved video file.

    Raises:
        ValueError: If parameters are invalid.
        RuntimeError: If generation or download fails.
        TimeoutError: If generation exceeds max_wait.

    Examples:
        >>> path = generate_video("A cat playing with yarn")
        'generated_video.mp4'

        >>> path = generate_video(
        ...     "Aerial drone shot of a coastline at golden hour",
        ...     model="sora-pro",
        ...     size="1280x720",
        ...     seconds=8,
        ...     output="coastline.mp4",
        ... )
        'coastline.mp4'
    """
    if verbose:
        print(f"🎬 Generating video with {resolve_model(model)}...")
        print(f"📝 Prompt: {prompt}")
        print(f"📐 Size: {size} | ⏱️ Duration: {seconds}s\n")

    # Step 1: Submit
    if verbose:
        print("Step 1: Submitting generation request...")
    video_id = submit_video(prompt, model=model, size=size, seconds=seconds)
    if verbose:
        print(f"  ✅ Video ID: {video_id[:60]}...\n")

    # Step 2: Poll
    if verbose:
        print("Step 2: Waiting for completion...")
    poll_video(video_id, max_wait=max_wait, verbose=verbose)
    if verbose:
        print()

    # Step 3: Download
    if verbose:
        print("Step 3: Downloading video...")
    path = download_video(video_id, output=output)
    file_size = Path(path).stat().st_size
    if verbose:
        print(f"  ✅ Saved to {path} ({file_size:,} bytes)\n")

    return path


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    print("=== Video Generation Utility Test ===\n")

    print("1. Submit + poll + download with sora-2:")
    try:
        path = generate_video(
            "A red ball bouncing on a white floor, simple animation",
            model="sora",
            size="1280x720",
            seconds=8,
            output="/tmp/test_video.mp4",
        )
        size = os.path.getsize(path)
        print(f"   ✅ Video saved: {path} ({size:,} bytes)\n")
        os.remove(path)
    except Exception as e:
        print(f"   ❌ Error: {e}\n")

    print("✅ Video tests complete!")

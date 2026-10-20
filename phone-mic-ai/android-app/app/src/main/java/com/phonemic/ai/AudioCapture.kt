package com.phonemic.ai

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor

/**
 * Reads the phone microphone as PCM 16-bit mono in 20 ms chunks.
 * VOICE_RECOGNITION gives clean, unprocessed audio tuned for speech-to-text.
 */
class AudioCapture(
    private val sampleRate: Int = StreamProtocol.SAMPLE_RATE,
    private val noiseSuppression: Boolean = true,
) {
    private var record: AudioRecord? = null
    private var ns: NoiseSuppressor? = null
    private var agc: AutomaticGainControl? = null

    val chunkBytes: Int = sampleRate * StreamProtocol.CHUNK_MS / 1000 * 2   // 16-bit mono

    @SuppressLint("MissingPermission") // checked in MainActivity before the service starts
    fun start() {
        if (record != null) return
        val minBuf = AudioRecord.getMinBufferSize(
            sampleRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        require(minBuf > 0) { "Microphone does not support $sampleRate Hz mono 16-bit" }
        val rec = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            maxOf(minBuf, chunkBytes * 8),
        )
        check(rec.state == AudioRecord.STATE_INITIALIZED) { "Microphone is busy or unavailable" }
        if (noiseSuppression && NoiseSuppressor.isAvailable()) ns = NoiseSuppressor.create(rec.audioSessionId)?.apply { enabled = true }
        if (AutomaticGainControl.isAvailable()) agc = AutomaticGainControl.create(rec.audioSessionId)?.apply { enabled = true }
        rec.startRecording()
        record = rec
    }

    /** Blocks until one full chunk is read. Returns bytes read (<= 0 on error). */
    fun read(buffer: ByteArray): Int {
        val rec = record ?: return -1
        var off = 0
        while (off < buffer.size) {
            val n = rec.read(buffer, off, buffer.size - off)
            if (n <= 0) return n
            off += n
        }
        return off
    }

    fun stop() {
        ns?.release(); ns = null
        agc?.release(); agc = null
        record?.let {
            try { it.stop() } catch (_: IllegalStateException) {}
            it.release()
        }
        record = null
    }
}

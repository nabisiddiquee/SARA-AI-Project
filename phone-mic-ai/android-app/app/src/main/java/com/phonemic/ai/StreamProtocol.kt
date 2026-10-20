package com.phonemic.ai

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.UUID

/**
 * Wire format shared with python-server/protocol.py:
 * 12-byte header ("PMIC", version, channels, bits, reserved, sampleRate u32 LE),
 * then raw PCM 16-bit little-endian until the connection closes.
 */
object StreamProtocol {
    /** RFCOMM service UUID advertised by the laptop. Must match protocol.SERVICE_UUID. */
    val SERVICE_UUID: UUID = UUID.fromString("8f4c2a10-6b3e-4f5d-9a7c-2e1b0d3c4a55")
    const val USB_PORT = 5000

    const val SAMPLE_RATE = 16_000      // 16 kHz: ideal for speech-to-text
    const val CHANNELS = 1              // mono
    const val BITS = 16                 // PCM 16-bit
    const val CHUNK_MS = 20             // 20 ms = 640 bytes at 16 kHz mono

    fun header(sampleRate: Int = SAMPLE_RATE, channels: Int = CHANNELS, bits: Int = BITS): ByteArray =
        ByteBuffer.allocate(12).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("PMIC".toByteArray(Charsets.US_ASCII))
            put(1)                       // version
            put(channels.toByte())
            put(bits.toByte())
            put(0)                       // reserved
            putInt(sampleRate)
        }.array()
}

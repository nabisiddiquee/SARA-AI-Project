package com.phonemic.ai

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothManager
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.ArrayAdapter
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.phonemic.ai.databinding.ActivityMainBinding
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {

    private lateinit var ui: ActivityMainBinding
    private var pairedAddresses: List<String> = emptyList()
    private val prefs by lazy { getSharedPreferences("phonemic", MODE_PRIVATE) }

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
            if (result[Manifest.permission.RECORD_AUDIO] == false) {
                Toast.makeText(this, "Microphone permission is required", Toast.LENGTH_LONG).show()
            }
            loadPairedDevices()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = ActivityMainBinding.inflate(layoutInflater)
        setContentView(ui.root)

        if (prefs.getString("mode", "bluetooth") == "usb") ui.modeUsb.isChecked = true else ui.modeBluetooth.isChecked = true
        ui.modeGroup.setOnCheckedChangeListener { _, _ -> updateModeUi() }
        ui.startButton.setOnClickListener { start() }
        ui.stopButton.setOnClickListener { stopMicService() }
        ui.refreshButton.setOnClickListener { requestPermissions() }

        requestPermissions()
        updateModeUi()

        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                MicStreamService.state.collect { s ->
                    ui.statusText.text = s.status
                    ui.levelBar.progress = ((s.levelDb + 60f) / 60f * 100f).toInt().coerceIn(0, 100)
                    ui.startButton.isEnabled = !s.running
                    ui.stopButton.isEnabled = s.running
                }
            }
        }
    }

    private fun requiredPermissions(): Array<String> = buildList {
        add(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) add(Manifest.permission.BLUETOOTH_CONNECT)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) add(Manifest.permission.POST_NOTIFICATIONS)
    }.toTypedArray()

    private fun granted(p: String) = ContextCompat.checkSelfPermission(this, p) == PackageManager.PERMISSION_GRANTED

    private fun requestPermissions() {
        val missing = requiredPermissions().filterNot(::granted)
        if (missing.isEmpty()) loadPairedDevices() else permissionLauncher.launch(missing.toTypedArray())
    }

    @SuppressLint("MissingPermission")
    private fun loadPairedDevices() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && !granted(Manifest.permission.BLUETOOTH_CONNECT)) return
        val adapter = getSystemService(BluetoothManager::class.java)?.adapter ?: return
        // Show computers first: that's where the Python server runs.
        val devices = adapter.bondedDevices.orEmpty()
            .sortedBy { if (it.bluetoothClass?.majorDeviceClass == android.bluetooth.BluetoothClass.Device.Major.COMPUTER) 0 else 1 }
        pairedAddresses = devices.map { it.address }
        val names = devices.map { "${it.name ?: "Unknown"}  (${it.address})" }
        ui.deviceSpinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item,
            names.ifEmpty { listOf("No paired devices: pair the laptop in Bluetooth settings") })
        val last = prefs.getString("address", null)
        pairedAddresses.indexOf(last).takeIf { it >= 0 }?.let { ui.deviceSpinner.setSelection(it) }
    }

    private fun updateModeUi() {
        val usb = ui.modeUsb.isChecked
        ui.deviceSpinner.isEnabled = !usb
        ui.hintText.text = if (usb) getString(R.string.hint_usb) else getString(R.string.hint_bluetooth)
    }

    private fun start() {
        if (!granted(Manifest.permission.RECORD_AUDIO)) { requestPermissions(); return }
        val usb = ui.modeUsb.isChecked
        val address = if (usb) null else pairedAddresses.getOrNull(ui.deviceSpinner.selectedItemPosition)
        if (!usb && address == null) {
            Toast.makeText(this, "Pair your laptop first, then pick it here", Toast.LENGTH_LONG).show()
            return
        }
        prefs.edit().putString("mode", if (usb) "usb" else "bluetooth").putString("address", address).apply()
        startMicService(if (usb) "usb" else "bluetooth", address)
    }
}

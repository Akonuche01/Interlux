import 'package:flutter/services.dart';

/// Device-API bridge (Phase 3.11): battery, clipboard, notifications,
/// haptics. No permissions to request on our targetSdk (install grants +
/// OS-handled clipboard toast). Built for app UI and the Stage-6 agent.
class DeviceApi {
  static const _channel = MethodChannel('interlux/device');

  /// Battery percentage 0-100, or -1 if unknown.
  static Future<int> batteryLevel() async {
    try {
      return await _channel.invokeMethod<int>('batteryLevel') ?? -1;
    } catch (_) {
      return -1;
    }
  }

  /// Current clipboard text, or null if empty/unavailable.
  static Future<String?> clipboardGet() async {
    try {
      return await _channel.invokeMethod<String>('clipboardGet');
    } catch (_) {
      return null;
    }
  }

  static Future<void> clipboardSet(String text) async {
    try {
      await _channel.invokeMethod<bool>('clipboardSet', {'text': text});
    } catch (_) {
      // Clipboard unavailable; not fatal.
    }
  }

  /// Post a local notification (auto-cancel on tap).
  static Future<void> notify(String title, String body) async {
    try {
      await _channel.invokeMethod<bool>('notify', {
        'title': title,
        'body': body,
      });
    } catch (_) {
      // Notifications unavailable; not fatal.
    }
  }

  /// Short haptic buzz (1-2000ms, clamped natively too).
  static Future<void> vibrate([int ms = 50]) async {
    try {
      await _channel.invokeMethod<bool>('vibrate', {'ms': ms});
    } catch (_) {
      // No vibrator; not fatal.
    }
  }

  /// Speak text aloud via the system TTS engine (queued, fire-and-forget).
  static Future<void> ttsSpeak(String text) async {
    try {
      await _channel.invokeMethod<bool>('ttsSpeak', {'text': text});
    } catch (_) {
      // No TTS engine; not fatal.
    }
  }

  static Future<void> ttsStop() async {
    try {
      await _channel.invokeMethod<bool>('ttsStop');
    } catch (_) {
      // No TTS engine; not fatal.
    }
  }

  /// Last-known fix as {lat, lon, acc}, or null when unavailable/denied.
  static Future<Map?> lastLocation() async {
    try {
      final m = await _channel.invokeMapMethod<String, double>(
        'lastLocation',
      );
      return m;
    } catch (_) {
      return null;
    }
  }

  /// Ask for location permission via the system dialog. Returns granted?
  static Future<bool> requestLocation() async {
    try {
      return await _channel.invokeMethod<bool>('requestLocation') ?? false;
    } catch (_) {
      return false;
    }
  }

  /// Capture a photo with the system camera app. Returns the saved file
  /// path, or null if cancelled/unavailable.
  static Future<String?> capturePhoto() async {
    try {
      return await _channel.invokeMethod<String>('capturePhoto');
    } catch (_) {
      return null;
    }
  }
}

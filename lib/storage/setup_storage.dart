import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Shared-storage prompt (Phase 1.3).
///
/// With targetSdk 28 the classic storage permissions still open all of
/// /sdcard, exposed in the shell as ~/storage (symlink made by the login
/// profile). Shown once; "Later" remembers the dismissal.
class StoragePrompt {
  static const _channel = MethodChannel('interlux/storage');
  static const _dismissKey = 'storage_prompt_dismissed';

  static Future<void> maybeShow(BuildContext context) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      if (prefs.getBool(_dismissKey) ?? false) return;
      final has =
          await _channel.invokeMethod<bool>('hasStorage') ?? true;
      if (has) return;
    } catch (_) {
      return;
    }
    if (!context.mounted) return;
    final action = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: const Color(0xFF1A1A1A),
        title: const Text(
          'Access shared storage?',
          style: TextStyle(color: Colors.white),
        ),
        content: const Text(
          'Interlux can expose your shared files inside the shell as '
          '~/storage (Downloads, documents, backups…). Nothing leaves '
          'the phone; the AI agent and scripts only see it if you ask.',
          style: TextStyle(color: Color(0xFFE6E6E6), fontSize: 13),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, 'later'),
            child: const Text('Later'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, 'allow'),
            child: const Text('Allow'),
          ),
        ],
      ),
    );
    final prefs = await SharedPreferences.getInstance();
    if (action == 'allow') {
      try {
        await _channel.invokeMethod<bool>('requestStorage');
      } catch (_) {
        // System dialog unavailable; stop asking.
      }
    }
    // Remember ANY dismissal, not just the two buttons. A barrier tap or Back
    // returns null, and the class doc promises this is "Shown once" -- so
    // leaving those unpersisted made an accidental tap outside the dialog
    // indistinguishable from "Later" and brought the prompt back on every
    // single cold start.
    await prefs.setBool(_dismissKey, true);
  }
}

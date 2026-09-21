import 'package:flutter/material.dart';

import 'terminal/terminal_screen.dart';

void main() {
  runApp(const InterluxApp());
}

class InterluxApp extends StatelessWidget {
  const InterluxApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Interlux',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.deepPurple),
      ),
      darkTheme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: Colors.deepPurple,
          brightness: Brightness.dark,
        ),
      ),
      themeMode: ThemeMode.dark,
      home: const TerminalScreen(),
    );
  }
}

import 'package:flutter/material.dart';

void main() {
  runApp(const TermCodeApp());
}

class TermCodeApp extends StatelessWidget {
  const TermCodeApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'TermCode',
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
      home: const HomePage(),
    );
  }
}

class HomePage extends StatelessWidget {
  const HomePage({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('TermCode')),
      body: const Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.terminal, size: 64),
            SizedBox(height: 16),
            Text('TermCode', style: TextStyle(fontSize: 24)),
            SizedBox(height: 8),
            Text('Terminal + Editor — scaffold ready'),
          ],
        ),
      ),
    );
  }
}

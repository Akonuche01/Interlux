import 'package:flutter/material.dart';

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
      home: const HomePage(),
    );
  }
}

class HomePage extends StatelessWidget {
  const HomePage({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Interlux')),
      body: const Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.terminal, size: 64),
            SizedBox(height: 16),
            Text('Interlux', style: TextStyle(fontSize: 24)),
            SizedBox(height: 8),
            Text('Terminal — scaffold ready'),
          ],
        ),
      ),
    );
  }
}

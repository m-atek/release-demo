// Store Floor: the app store staff carry on the shop floor. It shows its
// version in large type, so a camera can read which release a device runs,
// today's orders from orders-service, and since 2.4.0 the stock alerts.
//
// The build passes the version, the build number and the orders-service URL:
//   flutter build ipa --dart-define=APP_VERSION=2.4.0 --dart-define=APP_BUILD=57
//                     --dart-define=ORDERS_URL=https://...

import 'dart:async';

import 'package:flutter/material.dart';

import 'orders.dart';

/// The default is the VERSION file's; a test keeps the two the same.
const appVersion = String.fromEnvironment('APP_VERSION', defaultValue: '2.4.0');
const appBuild = String.fromEnvironment('APP_BUILD', defaultValue: 'dev');
const ordersUrl = String.fromEnvironment('ORDERS_URL');

void main() {
  runApp(StoreFloorApp(loader: ordersUrl.isEmpty ? null : httpOrders(ordersUrl)));
}

class StoreFloorApp extends StatelessWidget {
  const StoreFloorApp({
    super.key,
    this.loader,
    this.version = appVersion,
    this.buildNumber = appBuild,
    this.refresh = const Duration(seconds: 60),
  });

  /// Null when the build has no orders-service URL.
  final OrdersLoader? loader;
  final String version;
  final String buildNumber;
  final Duration refresh;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Store Floor',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF047857))),
      home: HomePage(loader: loader, version: version, buildNumber: buildNumber, refresh: refresh),
    );
  }
}

class HomePage extends StatefulWidget {
  const HomePage({
    super.key,
    required this.loader,
    required this.version,
    required this.buildNumber,
    required this.refresh,
  });

  final OrdersLoader? loader;
  final String version;
  final String buildNumber;
  final Duration refresh;

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  TodaysOrders? _orders;
  String? _error;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _load();
    if (widget.loader != null) {
      _timer = Timer.periodic(widget.refresh, (_) => _load());
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    final loader = widget.loader;
    if (loader == null) {
      setState(() => _error = 'This build has no orders-service address.');
      return;
    }
    try {
      final orders = await loader();
      if (mounted) {
        setState(() {
          _orders = orders;
          _error = null;
        });
      }
    } catch (error) {
      if (mounted) setState(() => _error = 'Orders unavailable: $error');
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final orders = _orders;
    return Scaffold(
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: _load,
          child: ListView(
            padding: const EdgeInsets.all(24),
            children: [
              Text('Store Floor', style: theme.textTheme.titleLarge),
              Text(
                widget.version,
                key: const Key('version'),
                style: theme.textTheme.displayLarge?.copyWith(
                  fontSize: 120,
                  fontWeight: FontWeight.w700,
                  color: theme.colorScheme.primary,
                ),
              ),
              Text('Build ${widget.buildNumber}', style: theme.textTheme.bodyLarge),
              const SizedBox(height: 24),
              if (_error != null) Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
              if (orders != null) ...[
                StockAlerts(alerts: stockAlerts(orders.orders)),
                const SizedBox(height: 24),
                OrdersList(orders: orders),
              ],
              const SizedBox(height: 24),
              Text("Demo system, not a client's.", style: theme.textTheme.bodySmall),
            ],
          ),
        ),
      ),
    );
  }
}

class StockAlerts extends StatelessWidget {
  const StockAlerts({super.key, required this.alerts});

  final List<StockAlert> alerts;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      color: theme.colorScheme.errorContainer,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Stock alerts', style: theme.textTheme.headlineSmall),
            const SizedBox(height: 8),
            if (alerts.isEmpty) const Text('No product runs low today.'),
            for (final alert in alerts)
              Text('${alert.product}: ordered ${alert.ordered} times today', style: theme.textTheme.titleMedium),
          ],
        ),
      ),
    );
  }
}

class OrdersList extends StatelessWidget {
  const OrdersList({super.key, required this.orders});

  final TodaysOrders orders;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text("Today's orders (${orders.orders.length})", style: theme.textTheme.headlineSmall),
        for (final order in orders.orders)
          ListTile(
            contentPadding: EdgeInsets.zero,
            title: Text('${order.id}  ${order.store}'),
            subtitle: Text(order.items.join(', ')),
            trailing: Text('€ ${order.totalEur.toStringAsFixed(2)}\n${order.status}', textAlign: TextAlign.right),
          ),
      ],
    );
  }
}

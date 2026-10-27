import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:store_floor/main.dart';
import 'package:store_floor/orders.dart';

const today = TodaysOrders(
  date: '2026-10-27',
  orders: [
    Order(id: '261027-001', store: 'Utrecht Centraal', items: ['Running shoes'], totalEur: 89.95, status: 'packed'),
    Order(id: '261027-002', store: 'Amsterdam Zuid', items: ['Running shoes'], totalEur: 89.95, status: 'received'),
    Order(
      id: '261027-003',
      store: 'Den Haag Spui',
      items: ['Running shoes', 'Water bottle'],
      totalEur: 102.90,
      status: 'shipped',
    ),
  ],
);

void main() {
  testWidgets('shows the version in large type, the orders and the stock alerts', (tester) async {
    await tester.pumpWidget(StoreFloorApp(loader: () async => today, version: '2.4.0', buildNumber: '57'));
    await tester.pumpAndSettle();

    expect(find.text('2.4.0'), findsOneWidget);
    expect(tester.widget<Text>(find.byKey(const Key('version'))).style?.fontSize, 120);
    expect(find.text('Build 57'), findsOneWidget);
    expect(find.text("Today's orders (3)"), findsOneWidget);
    expect(find.text('Stock alerts'), findsOneWidget);
    expect(find.text('Running shoes: ordered 3 times today'), findsOneWidget);
  });

  testWidgets('says so when orders-service fails', (tester) async {
    await tester.pumpWidget(StoreFloorApp(loader: () async => throw const HttpException('down')));
    await tester.pumpAndSettle();
    expect(find.textContaining('Orders unavailable'), findsOneWidget);
  });

  testWidgets('says so when the build has no orders-service address', (tester) async {
    await tester.pumpWidget(const StoreFloorApp());
    await tester.pumpAndSettle();
    expect(find.text('This build has no orders-service address.'), findsOneWidget);
  });

  test('the version the app shows by default is the one in VERSION and pubspec.yaml', () {
    final version = File('VERSION').readAsStringSync().trim();
    expect(appVersion, version);
    expect(
      RegExp(r'^version: (\S+)', multiLine: true).firstMatch(File('pubspec.yaml').readAsStringSync())!.group(1),
      '$version+1',
    );
  });
}

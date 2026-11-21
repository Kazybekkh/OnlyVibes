// Tab bar: floating Liquid Glass — translucent blur over the scrolling content,
// SF Symbol icons. Headers (with iOS large titles) live in each tab's own Stack.
import { BlurView } from 'expo-blur';
import { Tabs } from 'expo-router';
import { SymbolView, type SFSymbol } from 'expo-symbols';
import { StyleSheet, View } from 'react-native';
import { colors, glass } from '../../lib/theme';

function TabIcon({ name, color }: { name: SFSymbol; color: string }) {
  return <SymbolView name={name} size={24} tintColor={color} />;
}

export default function TabsLayout() {
  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        sceneStyle: { backgroundColor: colors.bg },
        tabBarActiveTintColor: colors.accent,
        tabBarInactiveTintColor: colors.gray,
        tabBarLabelStyle: { fontSize: 11, fontWeight: '600' },
        // iOS 26 floating Liquid Glass pill: capsule floating above the home
        // indicator with side margins; content scrolls underneath the blur.
        tabBarStyle: {
          position: 'absolute',
          left: 12,
          right: 12,
          bottom: 8,
          height: 62,
          backgroundColor: 'transparent',
          borderRadius: 999,
          borderWidth: StyleSheet.hairlineWidth,
          borderColor: colors.glassBorder,
          overflow: 'hidden',
          elevation: 0,
        },
        tabBarItemStyle: { paddingTop: 8 },
        tabBarBackground: () => (
          <>
            <BlurView intensity={glass.regular.intensity} tint="dark" style={StyleSheet.absoluteFill} />
            <View style={[StyleSheet.absoluteFill, { backgroundColor: glass.regular.fill }]} />
          </>
        ),
      }}
    >
      <Tabs.Screen
        name="fleet"
        options={{
          title: 'Fleet',
          tabBarIcon: ({ color }) => <TabIcon name="laptopcomputer" color={String(color)} />,
        }}
      />
      <Tabs.Screen
        name="alerts"
        options={{
          title: 'Alerts',
          tabBarIcon: ({ color }) => <TabIcon name="exclamationmark.triangle.fill" color={String(color)} />,
        }}
      />
      <Tabs.Screen
        name="settings"
        options={{
          title: 'Settings',
          tabBarIcon: ({ color }) => <TabIcon name="gearshape.fill" color={String(color)} />,
        }}
      />
    </Tabs>
  );
}

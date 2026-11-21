// Tab bar: floating Liquid Glass — translucent blur over the scrolling content,
// SF Symbol icons. Headers (with iOS large titles) live in each tab's own Stack.
import { BlurView } from 'expo-blur';
import { Tabs } from 'expo-router';
import type { SFSymbol } from 'expo-symbols';
import { StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { Icon } from '../../components/Icon';
import { colors, glass } from '../../lib/theme';

const TAB_BAR_HEIGHT = 62;

function TabIcon({ name, color }: { name: SFSymbol; color: string }) {
  return <Icon name={name} size={22} color={color} />;
}

export default function TabsLayout() {
  const insets = useSafeAreaInsets();
  return (
    <Tabs
      // the pill floats above the home indicator instead of padding into it
      safeAreaInsets={{ bottom: 0 }}
      screenOptions={{
        headerShown: false,
        sceneStyle: { backgroundColor: colors.bg },
        tabBarActiveTintColor: colors.accent,
        tabBarInactiveTintColor: colors.gray,
        tabBarLabelStyle: { fontSize: 11, fontWeight: '600' },
        // iOS 26/27 floating Liquid Glass pill: capsule with side margins,
        // content scrolls underneath the blur, lifted clear of the home indicator.
        tabBarStyle: {
          position: 'absolute',
          left: 16,
          right: 16,
          bottom: insets.bottom > 0 ? insets.bottom - 6 : 12,
          height: TAB_BAR_HEIGHT,
          paddingBottom: 0,
          backgroundColor: 'transparent',
          borderRadius: 999,
          borderWidth: StyleSheet.hairlineWidth,
          borderColor: colors.glassBorder,
          overflow: 'hidden',
          elevation: 0,
        },
        tabBarItemStyle: { height: TAB_BAR_HEIGHT, justifyContent: 'center' },
        tabBarBackground: () => (
          <>
            <BlurView intensity={glass.regular.intensity} tint="light" style={StyleSheet.absoluteFill} />
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
        name="board"
        options={{
          title: 'Board',
          tabBarIcon: ({ color }) => <TabIcon name="rectangle.grid.1x2.fill" color={String(color)} />,
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

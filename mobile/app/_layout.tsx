import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { colors } from '../lib/theme';

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <StatusBar style="light" />
      <Stack
        screenOptions={{
          headerShown: false,
          contentStyle: { backgroundColor: colors.bg },
        }}
      >
        <Stack.Screen name="(tabs)" />
        <Stack.Screen
          name="laptop/[key]"
          options={{
            presentation: 'card',
            headerShown: true,
            headerTransparent: true,
            headerTintColor: colors.label,
            headerTitleStyle: { fontWeight: '700', color: colors.label },
            headerBlurEffect: 'systemUltraThinMaterialDark',
            headerStyle: { backgroundColor: 'transparent' },
          }}
        />
      </Stack>
    </SafeAreaProvider>
  );
}

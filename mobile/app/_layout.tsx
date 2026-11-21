import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { headerOptions } from '../lib/headerOptions';
import { colors } from '../lib/theme';

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <StatusBar style="dark" />
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
            ...headerOptions,
            presentation: 'card',
            headerLargeTitle: false,
          }}
        />
      </Stack>
    </SafeAreaProvider>
  );
}

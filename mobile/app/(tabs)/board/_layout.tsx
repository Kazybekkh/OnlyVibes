import { Stack } from 'expo-router';
import { colors } from '../../../lib/theme';

export default function BoardStack() {
  return (
    <Stack
      screenOptions={{
        headerShown: true,
        headerTransparent: true,
        headerLargeTitle: true,
        headerLargeTitleShadowVisible: false,
        headerTintColor: colors.label,
        headerTitleStyle: { fontWeight: '700', color: colors.label },
        headerLargeTitleStyle: { color: colors.label },
        headerBlurEffect: 'systemUltraThinMaterialDark',
        headerStyle: { backgroundColor: 'transparent' },
        contentStyle: { backgroundColor: colors.bg },
      }}
    >
      <Stack.Screen name="index" options={{ title: 'Board', headerTitle: 'Board' }} />
    </Stack>
  );
}

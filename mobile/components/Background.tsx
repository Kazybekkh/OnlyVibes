// App backdrop: deep-space gradient with faint accent glows so the glass
// cards/tab bar have something to blur over (that's what makes it read as
// Liquid Glass instead of flat gray).
import { LinearGradient } from 'expo-linear-gradient';
import { StyleSheet, View } from 'react-native';
import { colors } from '../lib/theme';

export function Background() {
  return (
    <View style={StyleSheet.absoluteFill} pointerEvents="none">
      <LinearGradient
        colors={['#0b0e14', '#0d1322', '#0b0e14']}
        locations={[0, 0.55, 1]}
        style={StyleSheet.absoluteFill}
      />
      <View style={[styles.glow, { backgroundColor: '#0a84ff', top: -140, right: -120, opacity: 0.16 }]} />
      <View style={[styles.glow, { backgroundColor: '#7dd3fc', bottom: -180, left: -140, opacity: 0.10 }]} />
      <View style={[styles.glow, { backgroundColor: '#f0abfc', top: '38%', left: '55%', opacity: 0.07, width: 260, height: 260 }]} />
    </View>
  );
}

const styles = StyleSheet.create({
  glow: {
    position: 'absolute',
    width: 420,
    height: 420,
    borderRadius: 999,
  },
});

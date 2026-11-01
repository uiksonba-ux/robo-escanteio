package com.baskaraido.trader

/** Fixed, exhaustive percentage bands; no order execution or network access. */
object AccuracyRanges {
    fun indexFor(accuracy: Double): Int? {
        if (!accuracy.isFinite() || accuracy < 0.0 || accuracy > 100.0) return null
        return when {
            accuracy < 50.0 -> 0
            accuracy < 60.0 -> 1
            accuracy < 70.0 -> 2
            accuracy < 80.0 -> 3
            else -> 4
        }
    }
}

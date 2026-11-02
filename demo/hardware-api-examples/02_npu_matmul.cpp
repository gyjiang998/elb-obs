

    for (std::size_t row = 0; row < rows; ++row) {
        for (std::size_t column = 0; column < columns; ++column) {
            float accumulator = 0.0F;
            for (std::size_t k = 0; k < reduction; ++k) {
                accumulator += left[row * reduction + k] * right[k * columns + column];
            }
            output[row * columns + column] = accumulator;
        }
    }
